'use strict';
function log(m){console.log(m);}
var t0=Date.now(), stepTotal=3000+Math.floor(Math.random()*5000), lastStepBump=Date.now(), sampleCount=0;
function roomLux(t){return 300+40*Math.sin(2*Math.PI*0.02*t)+(Math.random()-0.5)*15;}
function proximityVal(){return (Math.random()<0.02)?0.0:5.0;}
Java.perform(function(){
    log("[*] sensors_extra.js loaded. Injecting light/proximity/step drift...");
    try {
        var Queue=Java.use("android.hardware.SystemSensorManager$SensorEventQueue");
        Queue.dispatchSensorEvent.overloads.forEach(function(ov){
            ov.implementation=function(){
                var args=arguments;
                try {
                    var t=(Date.now()-t0)/1000.0;
                    for(var i=0;i<args.length;i++){
                        var a=args[i];
                        if(a && a.length!==undefined && typeof a[0]==="number"){
                            if(a.length===1){
                                var v=a[0];
                                if(v>=1000 && Math.floor(v)===v){
                                    if(Date.now()-lastStepBump>6000){stepTotal+=1+Math.floor(Math.random()*3);lastStepBump=Date.now();}
                                    a[0]=stepTotal;
                                } else if(v<=10){ a[0]=proximityVal(); }
                                else { a[0]=roomLux(t); }
                                sampleCount++;
                                if(sampleCount%40===0){log("[EXTRA] single-sensor val -> "+a[0].toFixed(2)+"  (light/prox/step drift)");}
                            }
                            break;
                        }
                    }
                } catch(e){}
                return ov.apply(this,args);
            };
        });
        log("[+] Injecting drift into single-value sensors (light/proximity/step).");
    } catch(e){ log("[!] Hook failed: "+e); }
});
