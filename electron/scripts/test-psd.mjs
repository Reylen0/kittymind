// 测试 ag-psd 在 Node.js 里能否拿到像素数据
import { readPsd, initializeCanvas } from 'ag-psd'
import { readFileSync } from 'fs'
import { createCanvas } from 'canvas'

// 提供 canvas 实现给 ag-psd
initializeCanvas((w, h) => createCanvas(w, h))

const psd = readPsd(
  readFileSync('E:/class/roadmap/Agent/tool/_up_/public/models/deepseek.psd'),
  { skipCompositeImageData: true }
)

const layer = psd.children?.[0]
console.log('Layer name:', layer?.name)
console.log('Layer canvas type:', layer?.canvas?.constructor?.name)
console.log('Layer canvas width:', layer?.canvas?.width)
