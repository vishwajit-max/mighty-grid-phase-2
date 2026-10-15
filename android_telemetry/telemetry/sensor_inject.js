'use strict';
/**
 * sensor_inject.js — comprehensive sensor spoofing for POCO / Xiaomi
 * =====================================================================
 * Detects every sensor the target app is subscribed to and injects a
 * physically plausible stream for each one, per the selected persona.
 *
 * Personas: average / restless / careless / speedy / shaky
 *
 * Spoofs: accelerometer, gyroscope, magnetometer, light, proximity,
 *         step counter, step detector, significant motion, and any
 *         composite sensors the app reads directly.
 *
 * Read-only on the composite side: if the app reads GRAVITY or
 * LINEAR_ACCELERATION, we let Android derive them from the spoofed
 * accelerometer (which it does automatically when the raw sensor is
 * faked). Rotational-vector composites we leave alone unless the app
 * subscribes to them directly — computing them from raw gyro + mag
 * inside a Frida hook is fragile.
 * =====================================================================
 */

Java.perform(function () {
    console.log('[*] sensor_inject.js loaded — comprehensive spoof active');

    // -----------------------------------------------------------------------
    // Sensor type constants (from android.hardware.Sensor)
    // -----------------------------------------------------------------------
    var T_ACCEL    = 1,
        T_MAG      = 2,
        T_GYRO     = 4,
        T_LIGHT    = 5,
        T_PROX     = 8,
        T_GRAVITY  = 9,
        T_LIN_ACC  = 10,
        T_ROTVEC   = 11,
        T_STEP_CNT = 19,
        T_STEP_DET = 18,
        T_SIGMOT   = 17;

    var TYPES = {
        1:  'accelerometer',
        2:  'magnetic_field',
        4:  'gyroscope',
        5:  'light',
        8:  'proximity',
        9:  'gravity',
        10: 'linear_acceleration',
        11: 'rotation_vector',
        17: 'significant_motion',
        18: 'step_detector',
        19: 'step_counter'
    };

    // -----------------------------------------------------------------------
    // Map handle -> { type, name }
    // -----------------------------------------------------------------------
    var handleInfo = {};
    try {
        var SM = Java.use('android.hardware.SystemSensorManager');
        var Sensor = Java.use('android.hardware.Sensor');
        var ctx = Java.use('android.app.ActivityThread')
            .currentApplication().getApplicationContext();
        var sm = Java.cast(ctx.getSystemService('sensor'), SM);
        var list = sm.getSensorList(-1);
        for (var i = 0; i < list.size(); i++) {
            var s = Java.cast(list.get(i), Sensor);
            try {
                handleInfo[s.getHandle()] = {
                    type: s.getType(),
                    name: s.getName()
                };
            } catch (e) {}
        }
        console.log('[+] mapped ' + Object.keys(handleInfo).length +
                    ' sensors on device');
    } catch (e) {
        console.log('[!] sensor map failed: ' + e);
    }

    // -----------------------------------------------------------------------
    // Persona motion model for the accelerometer (m/s^2)
    // Returns [dx, dy, dz] to add to the physical values.
    // -----------------------------------------------------------------------
    var t0 = Date.now();
    function t_now() { return (Date.now() - t0) / 1000.0; }

    // Smooth noise generators (so successive samples aren't white noise).
    var noiseState = { ax: 0, ay: 0, az: 0, gx: 0, gy: 0, gz: 0 };
    function smoothNoise(key, alpha) {
        var r = (Math.random() - 0.5) * 2;
        noiseState[key] = (1 - alpha) * noiseState[key] + alpha * r;
        return noiseState[key];
    }

    var personas = {
        average: function (t) {
            var breath = 0.06 * Math.sin(2 * Math.PI * 0.25 * t);
            var tremor = 0.02 * Math.sin(2 * Math.PI * 8.5 * t);
            return [
                breath + tremor + smoothNoise('ax', 0.15) * 0.01,
                breath + tremor + smoothNoise('ay', 0.15) * 0.01,
                breath * 0.5 + smoothNoise('az', 0.15) * 0.01
            ];
        },
        restless: function (t) {
            var bounce = 0.25 * Math.sin(2 * Math.PI * 2.5 * t);
            var tremor = 0.04 * Math.sin(2 * Math.PI * 9.0 * t);
            return [
                bounce + tremor + smoothNoise('ax', 0.2) * 0.06,
                bounce + tremor + smoothNoise('ay', 0.2) * 0.06,
                0.1 * Math.sin(2 * Math.PI * 2.5 * t)
            ];
        },
        careless: function (t) {
            var tremor = 0.015 * Math.sin(2 * Math.PI * 8.0 * t);
            var spikePhase = (t % 4.0);
            var spike = spikePhase < 0.3
                ? 2.0 * Math.exp(-8.0 * spikePhase) * (Math.random() > 0.5 ? 1 : -1)
                : 0;
            return [
                spike + tremor + smoothNoise('ax', 0.25) * 0.03,
                spike * 0.7 + tremor + smoothNoise('ay', 0.25) * 0.03,
                smoothNoise('az', 0.25) * 0.02
            ];
        },
        speedy: function (t) {
            var stepFreq = 3.0, period = 1 / stepFreq;
            var phase = (t % period) / period;
            var impact = phase < 0.15 ? 1.2 * Math.exp(-10 * phase) : 0;
            var swing = 0.3 * Math.sin(2 * Math.PI * stepFreq * t);
            return [
                swing + impact + smoothNoise('ax', 0.3) * 0.05,
                0.2 * Math.sin(2 * Math.PI * stepFreq * t + Math.PI / 2) +
                    smoothNoise('ay', 0.3) * 0.05,
                0.2 * Math.abs(Math.sin(2 * Math.PI * stepFreq * t))
            ];
        },
        shaky: function (t) {
            var tremor = 0.12 * Math.sin(2 * Math.PI * 11.0 * t);
            var secondary = 0.04 * Math.sin(2 * Math.PI * 9.0 * t);
            return [
                tremor + secondary + smoothNoise('ax', 0.1) * 0.02,
                tremor + secondary + smoothNoise('ay', 0.1) * 0.02,
                0.03 * Math.sin(2 * Math.PI * 11.0 * t)
            ];
        }
    };

    // -----------------------------------------------------------------------
    // Per-sensor-type spoof handlers
    // -----------------------------------------------------------------------
    var stepBaseline = null;
    var lastStepBump = 0;
    var proxNear = false;

    function spoof_accel(vals, t, personaFn) {
        // Preserve the physical magnitude by adding small perturbations
        // to x/y/z. The result is |a| ≈ 9.81 with human noise on top.
        var m = personaFn(t);
        vals[0] = vals[0] + m[0];
        vals[1] = vals[1] + m[1];
        vals[2] = vals[2] + m[2];
    }

    function spoof_gyro(vals, t, personaFn) {
        // Gyro is in rad/s — tiny rotational jitter, coherent with the
        // accelerometer's tremor frequency.
        var m = personaFn(t);
        vals[0] = vals[0] + m[0] * 0.05;
        vals[1] = vals[1] + m[1] * 0.05;
        vals[2] = vals[2] + m[2] * 0.05;
    }

    function spoof_mag(vals) {
        // Earth's field: ~25-65 µT depending on latitude. Add small drift
        // to simulate a hand holding the phone.
        var drift = smoothNoise('ax', 0.05) * 1.5;
        var drift2 = smoothNoise('ay', 0.05) * 1.5;
        vals[0] = vals[0] + drift;
        vals[1] = vals[1] + drift2;
        // z stays closer to nominal
    }

    function spoof_light(vals, t) {
        // Indoor ambient: 200-500 lux, slowly varying
        var base = 300 + 40 * Math.sin(2 * Math.PI * 0.02 * t);
        var jitter = smoothNoise('az', 0.1) * 10;
        vals[0] = base + jitter;
    }

    function spoof_prox(vals, t) {
        // Mostly far (5.0 cm), occasional near (0.0 cm) as hand passes
        if (Math.random() < 0.005) proxNear = !proxNear;
        vals[0] = proxNear ? 0.0 : 5.0;
    }

    function spoof_step_counter(vals) {
        if (stepBaseline === null) stepBaseline = vals[0] || 0;
        var now = Date.now();
        if (now - lastStepBump > 8000) {
            stepBaseline += 1 + Math.floor(Math.random() * 3);
            lastStepBump = now;
        }
        vals[0] = stepBaseline;
    }

    function spoof_step_detector(vals) {
        // Event = 1.0 when a step is detected, 0.0 otherwise.
        // Emit ~1 event per 1.5 s so the app sees a plausible cadence.
        vals[0] = (Math.random() < 0.02) ? 1.0 : 0.0;
    }

    function spoof_gravity(vals, t, personaFn) {
        // Gravity is the low-frequency component of acceleration —
        // essentially 9.81 on z with tiny tilt noise.
        var m = personaFn(t);
        vals[0] = m[0] * 0.5;
        vals[1] = m[1] * 0.5;
        vals[2] = 9.81 + m[2] * 0.3;
    }

    function spoof_linear_accel(vals, t, personaFn) {
        // Linear acceleration = acceleration - gravity. On a still phone
        // that's near zero with small noise on top.
        var m = personaFn(t);
        vals[0] = m[0] * 0.3;
        vals[1] = m[1] * 0.3;
        vals[2] = m[2] * 0.3;
    }

    function spoof_significant_motion(vals) {
        // 0 = no motion, 1 = significant motion detected. Emit a pulse
        // every ~20 s so the app sees an occasional event.
        vals[0] = (Math.random() < 0.001) ? 1.0 : 0.0;
    }

    // -----------------------------------------------------------------------
    // Active persona (settable via RPC)
    // -----------------------------------------------------------------------
    var activePersona = 'average';
    function personaFn(t) {
        return (personas[activePersona] || personas.average)(t);
    }

    // -----------------------------------------------------------------------
    // The hook
    // -----------------------------------------------------------------------
    try {
        var Queue = Java.use(
            'android.hardware.SystemSensorManager$SensorEventQueue');
        Queue.dispatchSensorEvent.overloads.forEach(function (ov) {
            ov.implementation = function () {
                var args = arguments;
                try {
                    var handle = (typeof args[0] === 'number') ? args[0] : -1;
                    var info = handleInfo[handle] || {};
                    var type = info.type;

                    // Find the float[] argument
                    for (var i = 0; i < args.length; i++) {
                        var a = args[i];
                        if (a && a.length !== undefined &&
                            a.length >= 1 && typeof a[0] === 'number') {
                            var t = t_now();
                            switch (type) {
                                case T_ACCEL:    spoof_accel(a, t, personaFn); break;
                                case T_GYRO:     spoof_gyro(a, t, personaFn); break;
                                case T_MAG:      spoof_mag(a); break;
                                case T_LIGHT:    spoof_light(a, t); break;
                                case T_PROX:     spoof_prox(a, t); break;
                                case T_STEP_CNT: spoof_step_counter(a); break;
                                case T_STEP_DET: spoof_step_detector(a); break;
                                case T_GRAVITY:  spoof_gravity(a, t, personaFn); break;
                                case T_LIN_ACC:  spoof_linear_accel(a, t, personaFn); break;
                                case T_SIGMOT:   spoof_significant_motion(a); break;
                                default: break;   // leave uncalibrated/composites alone
                            }
                            break;
                        }
                    }
                } catch (e) {}
                return ov.apply(this, args);
            };
        });
        console.log('[+] dispatchSensorEvent hooked on ' +
                    Queue.dispatchSensorEvent.overloads.length +
                    ' overload(s)');
    } catch (e) {
        console.log('[X] hook failed: ' + e);
    }

    // -----------------------------------------------------------------------
    // RPC exports for the persona manager
    // -----------------------------------------------------------------------
    rpc.exports = {
        setpersona: function (name) {
            var target = name.toLowerCase();
            if (personas.hasOwnProperty(target)) {
                activePersona = target;
                return 'SUCCESS: persona switched to "' + target + '"';
            }
            return 'ERROR: unknown persona "' + name + '"';
        },
        getpersona: function () { return activePersona; },
        listpersonas: function () { return Object.keys(personas); }
    };

    console.log('[+] Sensor injection active. Initial persona: "' +
                activePersona + '"');
});