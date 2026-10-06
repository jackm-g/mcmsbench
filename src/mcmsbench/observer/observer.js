'use strict'
/**
 * The observer: one spectator mineflayer client that reads blocks for the bench, driven over stdio.
 *
 * Requests are JSON lines on stdin, {id, op, ...}; each gets one JSON line on stdout, {id, ok, result} or
 * {id, ok: false, error}. Anything else this process has to say goes to stderr. The observer never acts on the
 * world: it is put in spectator mode over RCON by the runner, and only reads what its client has loaded.
 *
 *   connect      {host, port, username, version}     -> {username}
 *   connected                                        -> bool
 *   disconnect                                       -> null
 *   position                                         -> [x, y, z] (floored)
 *   scan         {min: [x,y,z], max: [x,y,z], with_states}
 *                                                    -> {blocks: [[x,y,z,name]...], unloaded, states: [[x,y,z,{...}]...]}
 *   column_top   {x, z, y_top, y_bottom, mode}       -> [y, name] (mode: any | ground | floor)
 *   find_blocks  {names: [...], max_distance, count} -> [[x,y,z]...], nearest first
 *   watch_breaks {min, max}                          -> null; from now on every block in the box that goes from
 *                                                       solid/non-air to air or a liquid is recorded (by anyone)
 *   drain_breaks                                     -> [[t_ms, x, y, z, was]...] since the last drain
 */
const readline = require('readline')
const mineflayer = require('mineflayer')
const { Vec3 } = require('vec3')

// block states the graders read (rails, redstone, doors): the same set the scan has always returned
const STATEFUL = /rail|_stairs|door|trapdoor|lever|button|piston|redstone|repeater|comparator|observer|dispenser|dropper|hopper|note_block|tripwire|pressure_plate|fence_gate|torch|lamp|daylight_detector|bell/
const CANOPY = /_leaves$|^vine$|^glow_lichen$|^bamboo$|_mushroom_block$|^mushroom_stem$/
const AIR = new Set(['air', 'cave_air', 'void_air'])

let bot = null
let alive = false
let watchBox = null       // {min, max}: where breaks are recorded
let breaks = []
const BREAK_CAP = 20000
const EMPTY = /^(air|cave_air|void_air|water|lava|bubble_column)$/

function onBlockUpdate (oldBlock, newBlock) {
  if (!watchBox || !oldBlock || !newBlock) return
  const p = newBlock.position
  const { min, max } = watchBox
  if (p.x < min[0] || p.x > max[0] || p.y < min[1] || p.y > max[1] || p.z < min[2] || p.z > max[2]) return
  if (EMPTY.test(oldBlock.name) || !EMPTY.test(newBlock.name)) return
  if (breaks.length < BREAK_CAP) breaks.push([Date.now(), p.x, p.y, p.z, oldBlock.name])
}

function need () {
  if (!bot || !alive) throw new Error('observer is not connected')
  return bot
}

function connect ({ host, port, username, version, timeout_ms: timeoutMs }) {
  if (bot) { try { bot.quit() } catch (_) {} }
  bot = mineflayer.createBot({ host, port, username, version, auth: 'offline', hideErrors: true })
  alive = false
  const b = bot
  b.on('end', () => { if (bot === b) alive = false })
  b.on('blockUpdate', onBlockUpdate)
  b.on('error', (e) => process.stderr.write(`observer error: ${e && e.message}\n`))
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('observer: no spawn within ' + (timeoutMs || 30000) + ' ms')), timeoutMs || 30000)
    b.once('spawn', () => { clearTimeout(timer); alive = true; resolve({ username: b.username }) })
    b.once('kicked', (r) => { clearTimeout(timer); reject(new Error('kicked: ' + JSON.stringify(r))) })
    b.once('end', (r) => { clearTimeout(timer); reject(new Error('disconnected before spawn: ' + r)) })
  })
}

function scan ({ min, max, with_states: withStates }) {
  const b = need()
  const blocks = []
  const states = []
  let unloaded = 0
  const p = new Vec3(0, 0, 0)
  for (let x = min[0]; x <= max[0]; x++) {
    for (let z = min[2]; z <= max[2]; z++) {
      for (let y = min[1]; y <= max[1]; y++) {
        p.set(x, y, z)
        const blk = b.blockAt(p, false)
        if (!blk) { unloaded++; continue }
        if (blk.name === 'air') continue
        blocks.push([x, y, z, blk.name])
        if (withStates && STATEFUL.test(blk.name)) {
          let props = {}
          try { props = blk.getProperties() } catch (_) {}
          states.push([x, y, z, props])
        }
      }
    }
  }
  return { blocks, unloaded, states }
}

/** Highest block in a column: [y, name]; [null, 'unloaded'] / [null, null]. 'any' = solid or liquid; 'ground' = the
 * same ignoring tree canopy; 'floor' = solid ground only (the bottom under water). */
function columnTop ({ x, z, y_top: yTop, y_bottom: yBottom, mode }) {
  const b = need()
  mode = mode || 'any'
  const p = new Vec3(x, 0, z)
  for (let y = yTop; y >= yBottom; y--) {
    p.y = y
    const blk = b.blockAt(p, false)
    if (!blk) return [null, 'unloaded']
    if (AIR.has(blk.name)) continue
    if (mode !== 'any' && CANOPY.test(blk.name)) continue
    if (blk.boundingBox === 'block' || (mode !== 'floor' && /water|lava/.test(blk.name))) return [y, blk.name]
  }
  return [null, null]
}

function findBlocks ({ names, max_distance: maxDistance, count }) {
  const b = need()
  const ids = names.map((n) => b.registry.blocksByName[n]).filter(Boolean).map((blk) => blk.id)
  if (!ids.length) return []
  const me = b.entity.position
  return b.findBlocks({ matching: ids, maxDistance: maxDistance || 32, count: count || 10 })
    .sort((p, q) => me.distanceTo(p) - me.distanceTo(q))
    .map((p) => [p.x, p.y, p.z])
}

const OPS = {
  connect,
  connected: () => !!bot && alive,
  disconnect: () => { if (bot) { try { bot.quit() } catch (_) {} } bot = null; alive = false; return null },
  position: () => { const p = need().entity.position.floored(); return [p.x, p.y, p.z] },
  scan,
  column_top: columnTop,
  find_blocks: findBlocks,
  watch_breaks: ({ min, max }) => { watchBox = min && max ? { min, max } : null; breaks = []; return null },
  drain_breaks: () => { const out = breaks; breaks = []; return out }
}

const send = (o) => process.stdout.write(JSON.stringify(o) + '\n')

readline.createInterface({ input: process.stdin }).on('line', async (line) => {
  if (!line.trim()) return
  let req
  try { req = JSON.parse(line) } catch (e) { send({ id: null, ok: false, error: 'bad request: ' + e.message }); return }
  const fn = OPS[req.op]
  if (!fn) { send({ id: req.id, ok: false, error: 'unknown op ' + req.op }); return }
  try {
    send({ id: req.id, ok: true, result: await fn(req) })
  } catch (e) {
    send({ id: req.id, ok: false, error: (e && e.message) || String(e) })
  }
}).on('close', () => { if (bot) { try { bot.quit() } catch (_) {} } process.exit(0) })
