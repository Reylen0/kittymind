/**
 * 分析各图层实际有颜色的像素范围（忽略完全透明区域）
 */
import { readPsd, initializeCanvas } from 'ag-psd'
import { readFileSync } from 'fs'
import { join, dirname } from 'path'
import { fileURLToPath } from 'url'

const __dir = dirname(fileURLToPath(import.meta.url))

class MemContext2d {
  constructor(canvas) { this.canvas = canvas }
  putImageData(imageData, x = 0, y = 0) {
    const { width: cw, height: ch } = this.canvas
    const { width: iw, height: ih, data } = imageData
    if (!this.canvas._data) this.canvas._data = new Uint8ClampedArray(cw * ch * 4)
    for (let row = 0; row < ih; row++) {
      const srcOff = row * iw * 4
      const dstRow = y + row
      if (dstRow < 0 || dstRow >= ch) continue
      const dstOff = (dstRow * cw + x) * 4
      this.canvas._data.set(imageData.data.subarray(srcOff, srcOff + iw * 4), dstOff)
    }
  }
  createImageData(w, h) { return { width: w, height: h, data: new Uint8ClampedArray(w * h * 4) } }
  getImageData(x, y, w, h) {
    const out = this.createImageData(w, h)
    const cw = this.canvas.width
    for (let row = 0; row < h; row++) {
      const srcOff = ((y + row) * cw + x) * 4
      out.data.set(this.canvas._data.subarray(srcOff, srcOff + w * 4), row * w * 4)
    }
    return out
  }
}
class MemCanvas {
  constructor(w, h) { this.width = w; this.height = h; this._data = null }
  getContext() { return new MemContext2d(this) }
  toRGBA() { return this._data ?? new Uint8ClampedArray(this.width * this.height * 4) }
}
initializeCanvas((w, h) => new MemCanvas(w, h))

function pixelBounds(rgba, w, h, threshold = 10) {
  let minX = w, maxX = 0, minY = h, maxY = 0
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const alpha = rgba[(y * w + x) * 4 + 3]
      if (alpha > threshold) {
        if (x < minX) minX = x; if (x > maxX) maxX = x
        if (y < minY) minY = y; if (y > maxY) maxY = y
      }
    }
  }
  if (minX > maxX) return null
  return { minX, maxX, minY, maxY }
}

const psdPath = 'E:/class/roadmap/Agent/tool/_up_/public/models/deepseek.psd'
const psd = readPsd(readFileSync(psdPath), { skipCompositeImageData: true })
const { width: W, height: H } = psd

// 合成所有图层（找整体角色边界）
const composite = new Uint8ClampedArray(W * H * 4)

for (const layer of (psd.children ?? [])) {
  const c = layer.canvas
  if (!c) continue
  const rgba = c.toRGBA()
  // simple alpha compositing
  for (let i = 0; i < W * H; i++) {
    const a = rgba[i * 4 + 3]
    if (a > 0) {
      const alpha = a / 255
      for (let ch = 0; ch < 3; ch++) {
        composite[i * 4 + ch] = Math.round(rgba[i * 4 + ch] * alpha + composite[i * 4 + ch] * (1 - alpha))
      }
      composite[i * 4 + 3] = Math.min(255, composite[i * 4 + 3] + a)
    }
  }

  const b = pixelBounds(rgba, W, H)
  console.log(`  ${layer.name.padEnd(12)} bounds: (${b ? `${b.minX},${b.minY})-(${b.maxX},${b.maxY}` : 'empty'})`)
}

const total = pixelBounds(composite, W, H)
console.log(`\n全角色合成边界: (${total.minX},${total.minY})-(${total.maxX},${total.maxY})`)
console.log(`尺寸: ${total.maxX - total.minX + 1} × ${total.maxY - total.minY + 1}`)
