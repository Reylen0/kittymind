'use strict'
/**
 * 在没有外部依赖的情况下生成一个纯色 PNG 图标。
 * 只在 assets/tray-icon.png 不存在时调用一次。
 */
const zlib = require('zlib')
const fs   = require('fs')
const path = require('path')

function crc32(buf) {
  const t = new Uint32Array(256)
  for (let i = 0; i < 256; i++) {
    let c = i
    for (let j = 0; j < 8; j++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1)
    t[i] = c
  }
  let c = 0xFFFFFFFF
  for (const b of buf) c = t[(c ^ b) & 0xFF] ^ (c >>> 8)
  return (c ^ 0xFFFFFFFF) >>> 0
}

function pngChunk(type, data) {
  const len = Buffer.alloc(4)
  len.writeUInt32BE(data.length)
  const typeBuf = Buffer.from(type, 'ascii')
  const crcBuf  = Buffer.alloc(4)
  crcBuf.writeUInt32BE(crc32(Buffer.concat([typeBuf, data])))
  return Buffer.concat([len, typeBuf, data, crcBuf])
}

function solidPng(w, h, r, g, b) {
  const sig  = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10])

  const ihdrData = Buffer.alloc(13)
  ihdrData.writeUInt32BE(w, 0)
  ihdrData.writeUInt32BE(h, 4)
  ihdrData[8] = 8   // bit depth
  ihdrData[9] = 2   // color type: RGB
  const ihdr = pngChunk('IHDR', ihdrData)

  const rows = Buffer.concat(
    Array.from({ length: h }, () => {
      const row = Buffer.alloc(1 + w * 3)  // filter byte + RGB per pixel
      for (let x = 0; x < w; x++) {
        row[1 + x*3] = r
        row[2 + x*3] = g
        row[3 + x*3] = b
      }
      return row
    })
  )
  const idat = pngChunk('IDAT', zlib.deflateSync(rows))
  const iend = pngChunk('IEND', Buffer.alloc(0))

  return Buffer.concat([sig, ihdr, idat, iend])
}

/**
 * 如果 destPath 不存在，生成一个橙色 32×32 PNG 写入该路径。
 */
function ensureTrayIcon(destPath) {
  if (fs.existsSync(destPath)) return
  fs.mkdirSync(path.dirname(destPath), { recursive: true })
  fs.writeFileSync(destPath, solidPng(32, 32, 251, 146, 60))  // #FB923C orange
}

module.exports = { ensureTrayIcon }
