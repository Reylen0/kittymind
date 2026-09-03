import { readPsd, initializeCanvas } from 'ag-psd'
import { readFileSync } from 'fs'

class MC {
  constructor(w,h){this.w=w;this.h=h;this.d=null}
  getContext(){return{
    putImageData:(id,x,y)=>{if(!this.d)this.d=new Uint8ClampedArray(this.w*this.h*4);for(let r=0;r<id.height;r++){const s=r*id.width*4,D=((y+r)*this.w+x)*4;this.d.set(id.data.subarray(s,s+id.width*4),D)}},
    createImageData:(w,h)=>({width:w,height:h,data:new Uint8ClampedArray(w*h*4)})
  }}
  toRGBA(){return this.d??new Uint8ClampedArray(this.w*this.h*4)}
}
initializeCanvas((w,h)=>new MC(w,h))

const psd = readPsd(readFileSync('E:/class/roadmap/Agent/tool/_up_/public/models/deepseek.psd'),{skipCompositeImageData:true})
const W=psd.width, H=psd.height

// 取各层 alpha 最大值合成，找可见角色边界
const maxA = new Uint8Array(W*H)
for(const l of psd.children??[]){
  const r=l.canvas?.toRGBA(); if(!r) continue
  for(let i=0;i<W*H;i++) if(r[i*4+3]>maxA[i]) maxA[i]=r[i*4+3]
}

for(const thr of [30, 60, 100]){
  let mx=W,Mx=0,my=H,My=0
  for(let y=0;y<H;y++) for(let x=0;x<W;x++) if(maxA[y*W+x]>thr){
    if(x<mx)mx=x; if(x>Mx)Mx=x; if(y<my)my=y; if(y>My)My=y
  }
  console.log(`alpha>${thr}: x=${mx}-${Mx} (w=${Mx-mx+1}), y=${my}-${My} (h=${My-my+1})`)
}
