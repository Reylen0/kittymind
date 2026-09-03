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

/**
 * 如果 destPath 不存在，生成一个 256×256 PNG-in-ICO 写入该路径。
 * Windows 应用图标格式：ICO header + ICONDIRENTRY + PNG data。
 */
function ensureAppIcon(destPath) {
  if (fs.existsSync(destPath)) return
  fs.mkdirSync(path.dirname(destPath), { recursive: true })

  const png = solidPng(256, 256, 251, 146, 60)  // #FB923C orange

  // ICONDIR header (6 bytes)
  const header = Buffer.alloc(6)
  header.writeUInt16LE(0, 0)  // reserved
  header.writeUInt16LE(1, 2)  // type = 1 (ICO)
  header.writeUInt16LE(1, 4)  // count = 1 image

  // ICONDIRENTRY (16 bytes)
  const entry = Buffer.alloc(16)
  entry[0] = 0   // width:  0 = 256
  entry[1] = 0   // height: 0 = 256
  entry[2] = 0   // color count (0 for 32-bit)
  entry[3] = 0   // reserved
  entry.writeUInt16LE(1,  4)              // planes
  entry.writeUInt16LE(32, 6)              // bit count
  entry.writeUInt32LE(png.length, 8)      // image data size
  entry.writeUInt32LE(6 + 16, 12)         // image data offset

  fs.writeFileSync(destPath, Buffer.concat([header, entry, png]))
}

module.exports = { ensureTrayIcon, ensureAppIcon }
