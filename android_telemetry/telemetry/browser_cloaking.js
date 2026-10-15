/**
 * browser_cloaking.js
 * Injected at document_start via CDP or Web Engine Preload.
 * Cloaks Browser Fingerprints, DOM APIs, Hardware Identifiers, Timers, and Engine Telemetry.
 */

(function () {
    'use strict';

    // Helper: Mask functions to appear as native code
    const nativeToString = Function.prototype.toString;
    const hookedFunctions = new WeakSet();

    function makeNative(fn, name) {
        if (name) {
            Object.defineProperty(fn, 'name', { value: name, configurable: true });
        }
        hookedFunctions.add(fn);
        return fn;
    }

    Function.prototype.toString = function () {
        if (hookedFunctions.has(this)) {
            return `function ${this.name || ''}() { [native code] }`;
        }
        return nativeToString.apply(this, arguments);
    };
    hookedFunctions.add(Function.prototype.toString);

    // 1. Remove navigator.webdriver
    try {
        delete Navigator.prototype.webdriver;
        delete navigator.webdriver;
    } catch (e) {}

        // 2 & 3. Hardware Concurrency & Device Memory — driven by the selected
    // device profile. The pipeline sets window.__MG_PROFILE before this
    // script runs (see background_cdp_worker). If unset, falls back to
    // sensible defaults.
    const defineGetter = (proto, prop, val) => {
        const getter = makeNative(function () { return val; }, `get ${prop}`);
        Object.defineProperty(proto, prop, {
            get: getter, set: undefined, enumerable: true, configurable: true
        });
    };

    const _profile = window.__MG_PROFILE || {};
    const _cores = parseInt(_profile.hardwareConcurrency, 10) || 8;
    const _ram_gb = parseInt(_profile.deviceMemory, 10) || 4;

    defineGetter(Navigator.prototype, 'hardwareConcurrency', _cores);
    defineGetter(Navigator.prototype, 'deviceMemory', _ram_gb);

    // 4. Network Information API
    if ('connection' in navigator) {
        const connectionProps = { effectiveType: '4g', rtt: 50, downlink: 10.5, saveData: false };
        for (const [key, val] of Object.entries(connectionProps)) {
            try { defineGetter(Object.getPrototypeOf(navigator.connection), key, val); } catch (e) {}
        }
    }

       // 5. WebGL Vendor & Renderer — driven by the profile when provided;
    // defaults preserve the original M04 strings.
    const TARGET_VENDOR = _profile.glVendor || "Imagination Technologies";
    const TARGET_RENDERER = _profile.glRenderer || "PowerVR Rogue GE8320";

    function patchWebGL(targetProto) {
        if (!targetProto) return;
        const originalGetParameter = targetProto.getParameter;
        const hookedGetParameter = makeNative(function (param) {
            if (param === 37445) return TARGET_VENDOR;    
            if (param === 37446) return TARGET_RENDERER;  
            return originalGetParameter.apply(this, arguments);
        }, 'getParameter');
        targetProto.getParameter = hookedGetParameter;
    }
    if (window.WebGLRenderingContext) patchWebGL(WebGLRenderingContext.prototype);
    if (window.WebGL2RenderingContext) patchWebGL(WebGL2RenderingContext.prototype);

    // 6. Canvas 2D Deterministic Session Noise
    const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
    const hookedGetImageData = makeNative(function (x, y, w, h) {
        const imgData = origGetImageData.apply(this, arguments);
        for (let i = 0; i < imgData.data.length; i += 200) {
            imgData.data[i] = imgData.data[i] ^ 1; 
        }
        return imgData;
    }, 'getImageData');
    CanvasRenderingContext2D.prototype.getImageData = hookedGetImageData;

    // 7. Client Hints
    if ('userAgentData' in navigator) {
                const highEntropyValues = {
            architecture: "arm", bitness: "64",
            brands: [ { brand: "Chromium", version: "152" }, { brand: "Google Chrome", version: "152" }, { brand: "Not-A.Brand", version: "24" } ],
            mobile: true,
            model: _profile.Model || "SM-M045F",
            platform: "Android",
            platformVersion: "14.0.0"
        };
        const hookedGetHighEntropyValues = makeNative(function (hints) {
            return new Promise((resolve) => {
                const res = { brands: highEntropyValues.brands, mobile: highEntropyValues.mobile, platform: highEntropyValues.platform };
                hints.forEach(h => { if (highEntropyValues[h] !== undefined) res[h] = highEntropyValues[h]; });
                resolve(res);
            });
        }, 'getHighEntropyValues');

        try {
            const uaDataProto = Object.getPrototypeOf(navigator.userAgentData);
            uaDataProto.getHighEntropyValues = hookedGetHighEntropyValues;
            defineGetter(uaDataProto, 'mobile', true);
            defineGetter(uaDataProto, 'platform', 'Android');
        } catch (e) {}
    }

    // 8. Timer Jitter
    if (window.performance && typeof window.performance.now === 'function') {
        const originalNow = window.performance.now.bind(window.performance);
        window.__mg_jitter_active = true;
        const hookedNow = makeNative(function now() {
            return originalNow() + ((Math.random() * 0.2) - 0.1);
        }, 'now');
        Object.defineProperty(window.performance, 'now', {
            value: hookedNow, configurable: true, writable: true
        });
    }

    // -----------------------------------------------------------------------
    // 9. V8 ENGINE & RENDERING STRESSOR (Safe Emulation)
    // -----------------------------------------------------------------------
    
    // A. DOM Node Bloat (reduced: 1505 -> 400 nodes)
    function applyEngineBloat() {
        if (!document.body) {
            setTimeout(applyEngineBloat, 50);
            return;
        }
        if (document.getElementById('mg-engine-stressor')) return;
        
        const bloatContainer = document.createElement('div');
        bloatContainer.id = "mg-engine-stressor";
        bloatContainer.style.cssText = "position:absolute; width:1px; height:1px; overflow:hidden; opacity:0.01; pointer-events:none; left:-9999px;";
        
        let htmlStr = "";
        for(let i=0; i<400; i++) { htmlStr += "<span>_</span>"; }
        bloatContainer.innerHTML = htmlStr;
        document.body.appendChild(bloatContainer);
        window.__mg_dom_bloat_active = true;
    }
    applyEngineBloat();

    // B. Heap Memory Bloat & GC Trigger (reduced interval, smaller arrays)
    window.__mg_junk_memory = [];
    setInterval(() => {
        window.__mg_junk_memory.push(new Array(4000).fill(1));
        if (window.__mg_junk_memory.length > 4) {
            window.__mg_junk_memory = [];
        }
    }, 800);

    // C. Frame Dropper (reduced: 8ms max block, 8% chance, every 250ms)
    //    The old 25ms/15%/100ms combination pushed CDP responses past the
    //    5-second client timeout, which is why read-backs came back empty.
    window.__mg_frames_rendered = 0;
    setInterval(() => {
        window.__mg_frames_rendered++;
        if (Math.random() < 0.08) {
            const start = Date.now();
            while (Date.now() - start < 8) {
                /* Busy wait block */
            }
        }
    }, 250);
})();