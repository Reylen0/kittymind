// 检查 PSD 图层结构
import { readPsd } from 'ag-psd'
import { readFileSync } from 'fs'
import { join, dirname } from 'path'
import { fileURLToPath } from 'url'

const __dir = dirname(fileURLToPath(import.meta.url))
const psdPath = process.argv[2] || join(__dir, '../../tool/_up_/public/models/Elaina.psd')

console.log('Reading:', psdPath)
const buf = readFileSync(psdPath)
const psd = readPsd(buf, { skipLayerImageData: true, skipCompositeImageData: true })

console.log(`\nCanvas: ${psd.width} x ${psd.height}`)
console.log(`Channels: ${psd.channels}, Bit depth: ${psd.bitsPerChannel}`)
console.log(`\nLayers (${psd.children?.length ?? 0} top-level):\n`)

function printLayer(layer, depth = 0) {
  const indent = '  '.repeat(depth)
  const type   = layer.children ? '[GROUP]' : '[LAYER]'
  const vis    = layer.hidden ? 'hidden' : 'visible'
  const rect   = layer.left != null
    ? `(${layer.left},${layer.top})-(${layer.right},${layer.bottom})`
    : ''
  console.log(`${indent}${type} "${layer.name}" [${vis}] ${rect}`)
  if (layer.children) layer.children.forEach(c => printLayer(c, depth + 1))
}

psd.children?.forEach(c => printLayer(c))
