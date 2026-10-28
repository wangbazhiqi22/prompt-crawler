/* Soft bubble: a native WebGL implementation written for Prompt Crawler.
 * Matches the selected earlier UI's soft silhouette, face and warm/cool colors.
 * No imported artwork assets, shader sources, node graphs or rendering libraries.
 */
(() => {
  'use strict';
  const canvas = document.getElementById('ambientCanvas');
  const origin = new URL(location.href).origin;
  const layout = new URLSearchParams(location.search).get('layout') || 'workspace';
  const gl = canvas.getContext('webgl', {alpha: false, antialias: true, powerPreference: 'high-performance'});
  const send = (type, detail = {}) => { if (parent !== window) parent.postMessage({type, ...detail}, '*'); };
  if (!gl) { send('ambient:error'); return; }
  let paused = new URLSearchParams(location.search).get('poster') === '1';
  let lost = false, raf = 0, previous = 0, time = 0, pulse = 0;
  let capture = null, ready = false, frames = 0, intervals = [];
  const target = [0, 0], pointer = [0, 0];
  const uniforms = new Map();
  function program(vertex, fragment) {
    const p = gl.createProgram();
    for (const [type, source] of [[gl.VERTEX_SHADER, vertex], [gl.FRAGMENT_SHADER, fragment]]) {
      const s = gl.createShader(type); gl.shaderSource(s, source); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw Error(gl.getShaderInfoLog(s));
      gl.attachShader(p, s); gl.deleteShader(s);
    }
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw Error(gl.getProgramInfoLog(p));
    return p;
  }
  function uniform(p, name) {
    if (!uniforms.has(p)) uniforms.set(p, new Map());
    const cache = uniforms.get(p);
    if (!cache.has(name)) cache.set(name, gl.getUniformLocation(p, name));
    return cache.get(name);
  }
  function buffer(values, kind = gl.ARRAY_BUFFER) {
    const b = gl.createBuffer(); gl.bindBuffer(kind, b); gl.bufferData(kind, values, gl.STATIC_DRAW); return b;
  }
  function geometry(rows, columns) {
    const uv = [], index = [];
    for (let i = 0; i <= rows; i++) for (let j = 0; j <= columns; j++) uv.push(i / rows, j / columns);
    for (let i = 0; i < rows; i++) for (let j = 0; j < columns; j++) {
      const a = i * (columns + 1) + j, b = a + columns + 1;
      index.push(a, b, a + 1, b, b + 1, a + 1);
    }
    return {vertices: buffer(new Float32Array(uv)), indices: buffer(new Uint16Array(index), gl.ELEMENT_ARRAY_BUFFER), count: index.length};
  }
  const backgroundVertex = `
    attribute vec2 point; varying vec2 uv;
    void main(){ uv=point*.5+.5; gl_Position=vec4(point,0.,1.); }
  `;
  const backgroundFragment = `
    precision highp float;
    varying vec2 uv; uniform vec2 pointer; uniform float time;
    void main(){
      vec2 p=uv+pointer*.012;
      vec2 glow=vec2(.48+.07*sin(time*.11),.52+.05*cos(time*.13));
      float a=exp(-6.*dot(p-glow,p-glow));
      float b=exp(-14.*dot(p-vec2(.76,.2),p-vec2(.76,.2)));
      float c=exp(-10.*dot(p-vec2(.17,.83),p-vec2(.17,.83)));
      vec3 color=vec3(.82,.77,.98);
      color=mix(color,vec3(1.,.89,.95),a*.88);
      color=mix(color,vec3(.94,.78,.95),b*.55);
      color=mix(color,vec3(.89,.78,.98),c*.46);
      gl_FragColor=vec4(color,1.);
    }
  `;
  const sculptureVertex = `
    precision highp float;
    attribute vec2 param;
    uniform float time, pulse, kind, scale;
    uniform vec2 pointer, aspect, offset;
    varying vec3 normal, position;
    const float PI=3.141592653589793;
    mat3 rotateX(float a){float c=cos(a),s=sin(a);return mat3(1.,0.,0.,0.,c,s,0.,-s,c);}
    mat3 rotateY(float a){float c=cos(a),s=sin(a);return mat3(c,0.,-s,0.,1.,0.,s,0.,c);}
    mat3 rotateZ(float a){float c=cos(a),s=sin(a);return mat3(c,s,0.,-s,c,0.,0.,0.,1.);}
    float radius(vec3 d){
      return 1.+.065*sin(d.y*4.8+1.2+time*.65)*sin(d.x*3.6-.7+time*.37)
        +.045*cos(d.x*4.1-d.y*2.8+d.z*1.2-time*.55);
    }
    vec3 shape(vec3 d){
      vec3 p=d*radius(d);
      p.x*=1.+pulse*.055; p.y*=1.-pulse*.045;
      return p;
    }
    void main(){
      float u=param.x*PI*2.,lat=param.y*PI;
      vec3 d=vec3(sin(lat)*cos(u),cos(lat),sin(lat)*sin(u));
      vec3 p,n;
      mat3 turn=rotateY(pointer.x*.20)*rotateX(-pointer.y*.15)*rotateZ(sin(time*.28)*.025);
      if(kind<.5||kind>3.5){
        p=shape(d); n=d;
        if(kind>3.5)p*=1.035;
      }else{
        vec2 face=kind<1.5?vec2(-.175,-.065):kind<2.5?vec2(.175,-.065):vec2(0.,-.225);
        face+=vec2(-.055,-.01)+pointer*.035;
        vec3 anchor=shape(vec3(face,sqrt(max(.01,1.-dot(face,face)))));
        float blink=1.-.9*exp(-pow((mod(time+2.,7.7)-7.35)*17.,2.));
        vec3 size=kind<2.5?vec3(.082,.116*blink,.032):vec3(.028,.016,.018);
        p=anchor+d*size+vec3(0.,0.,.018); n=d;
      }
      position=p; p=turn*p; normal=turn*n;
      p.y+=.025*sin(time*.65);
      gl_Position=vec4(p.xy*aspect*scale+offset,-p.z*.2,1.);
    }
  `;
  const sculptureFragment = `
    precision highp float;
    varying vec3 normal, position;
    uniform float time,pulse,kind;
    void main(){
      vec3 n=normalize(normal);
      float facing=abs(n.z);
      if(kind>.5&&kind<3.5){gl_FragColor=vec4(vec3(.10,.10,.20)+facing*.045,1.);return;}
      if(kind>3.5){
        vec3 halo=mix(vec3(.55,1.,1.),vec3(1.,.97,1.),smoothstep(-.8,.1,position.y));
        float alpha=smoothstep(0.,.16,facing)*.38;
        gl_FragColor=vec4(halo,alpha);return;
      }
      float band=.5-.5*position.y+.055*sin(position.x*3.+time*.35);
      float cycle=.5-.5*cos(time*.15);
      // Travel around the hue wheel, rather than averaging complementary RGB colors.
      float hue=.075-cycle*.55+mix(-.22,.20,cycle)*smoothstep(.45,.98,band);
      float saturation=mix(.89,.50,smoothstep(.32,.78,band));
      vec3 spectrum=clamp(abs(mod(hue*6.+vec3(0.,4.,2.),6.)-3.)-1.,0.,1.);
      vec3 color=mix(vec3(1.),spectrum,saturation);
      float rim=pow(1.-facing,3.);
      vec3 edge=mix(vec3(.3,1.,1.),vec3(1.,.84,.94),smoothstep(-.85,.1,position.y));
      color=mix(color,edge,rim*.85);
      color=mix(color,vec3(1.),1.-smoothstep(.08,.23,facing));
      gl_FragColor=vec4(color,1.);
    }
  `;
  let backdrop, sculpture, quad, body, face;
  function initialize() {
    uniforms.clear();
    backdrop=program(backgroundVertex,backgroundFragment);
    sculpture=program(sculptureVertex,sculptureFragment);
    quad=buffer(new Float32Array([-1,-1,1,-1,-1,1,-1,1,1,-1,1,1]));
    body=geometry(128,72); face=geometry(40,28);
    // All meshes wind outward. Skip the hidden hemisphere instead of shading it twice.
    gl.clearColor(.92,.88,.97,1); gl.enable(gl.CULL_FACE); gl.cullFace(gl.BACK);
    resize();
  }
  function resize() {
    const ratio=Math.min(2,2560/Math.max(innerWidth,innerHeight));
    canvas.width=Math.max(1,Math.round(innerWidth*ratio));
    canvas.height=Math.max(1,Math.round(innerHeight*ratio));
    gl.viewport(0,0,canvas.width,canvas.height);
    canvas.dataset.resolution=`${canvas.width}×${canvas.height}`;
    if(paused&&!lost)render(performance.now());
  }
  function attribute(p, name, b) {
    const location=gl.getAttribLocation(p,name);
    gl.bindBuffer(gl.ARRAY_BUFFER,b); gl.enableVertexAttribArray(location); gl.vertexAttribPointer(location,2,gl.FLOAT,false,0,0);
  }
  function drawMesh(mesh, kind) {
    gl.uniform1f(uniform(sculpture,'kind'),kind);
    attribute(sculpture,'param',mesh.vertices); gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER,mesh.indices);
    gl.drawElements(gl.TRIANGLES,mesh.count,gl.UNSIGNED_SHORT,0);
  }
  function render(now) {
    raf=0; if(lost)return;
    const dt=previous?Math.min((now-previous)/1000,.05):0;
    if(!paused){time+=dt; pulse*=Math.exp(-dt*2.6);}
    for(let i=0;i<2;i++)pointer[i]+=(target[i]-pointer[i])*(1-Math.exp(-dt*5));
    if(previous&&!paused)intervals.push(now-previous); previous=now;
    gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT); gl.disable(gl.DEPTH_TEST);
    gl.useProgram(backdrop); attribute(backdrop,'point',quad);
    gl.uniform2f(uniform(backdrop,'pointer'),pointer[0],pointer[1]); gl.uniform1f(uniform(backdrop,'time'),time);
    gl.drawArrays(gl.TRIANGLES,0,6);
    gl.enable(gl.DEPTH_TEST); gl.useProgram(sculpture);
    const short=Math.min(canvas.width,canvas.height);
    gl.uniform2f(uniform(sculpture,'aspect'),short/canvas.width,short/canvas.height);
    const portrait=innerWidth/innerHeight<1.;
    gl.uniform2f(uniform(sculpture,'offset'),0,0);
    gl.uniform1f(uniform(sculpture,'scale'),layout==='login'?(portrait?1.35:.68):.62);
    gl.uniform2f(uniform(sculpture,'pointer'),pointer[0],pointer[1]);
    gl.uniform1f(uniform(sculpture,'time'),time); gl.uniform1f(uniform(sculpture,'pulse'),pulse);
    gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA,gl.ONE_MINUS_SRC_ALPHA); gl.depthMask(false);
    drawMesh(body,4);
    gl.depthMask(true); gl.disable(gl.BLEND);
    drawMesh(body,0); drawMesh(face,1); drawMesh(face,2); drawMesh(face,3);
    frames++; canvas.dataset.frame=String(frames);
    if(!ready){ready=true;canvas.dataset.ready='true';send('ambient:ready');}
    if(intervals.length>=90){
      const ordered=[...intervals].sort((a,b)=>a-b);
      canvas.dataset.fps=(1000/(intervals.reduce((a,b)=>a+b,0)/intervals.length)).toFixed(1);
      canvas.dataset.frameP95=ordered[Math.floor(ordered.length*.95)].toFixed(1); intervals=[];
    }
    if(capture!==null){const id=capture;capture=null;send('ambient:snapshot',{id,image:canvas.toDataURL('image/jpeg',.9)});}
    if(!paused)raf=requestAnimationFrame(render);
  }
  function setPaused(value){
    if(paused===value)return;
    paused=value; canvas.dataset.paused=String(value); previous=0;intervals=[];
    if(raf)cancelAnimationFrame(raf);raf=0;
    if(!paused&&!lost)raf=requestAnimationFrame(render);
  }
  canvas.addEventListener('pointermove',event=>{
    target[0]=event.clientX/innerWidth*2-1;target[1]=1-event.clientY/innerHeight*2;
  },{passive:true});
  canvas.addEventListener('pointerleave',()=>{target[0]=target[1]=0;});
  canvas.addEventListener('pointerdown',()=>{if(!paused){pulse=1;canvas.dataset.interactions=String(Number(canvas.dataset.interactions||0)+1);}},{passive:true});
  canvas.addEventListener('keydown',event=>{
    if(event.code==='Space'){event.preventDefault();if(parent===window)setPaused(!paused);else send('ambient:toggle');}
  });
  window.addEventListener('message',event=>{
    if(event.source!==parent||event.origin!==origin)return;
    if(event.data?.type==='ambient:pause')setPaused(true);
    if(event.data?.type==='ambient:resume')setPaused(false);
    if(event.data?.type==='ambient:capture'){capture=event.data.id;if(paused)render(performance.now());}
  });
  window.addEventListener('resize',resize,{passive:true});
  document.addEventListener('visibilitychange',()=>{if(parent===window)setPaused(document.hidden);});
  canvas.addEventListener('webglcontextlost',event=>{event.preventDefault();lost=true;if(raf)cancelAnimationFrame(raf);send('ambient:error');});
  canvas.addEventListener('webglcontextrestored',()=>{try{lost=false;ready=false;previous=0;initialize();render(performance.now());}catch(error){send('ambient:error');}});
  try {initialize();canvas.dataset.paused=String(paused);raf=requestAnimationFrame(render);}
  catch(error){console.error('Soft bubble renderer:',error.message);send('ambient:error');}
})();
