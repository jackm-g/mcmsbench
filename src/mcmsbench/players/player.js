'use strict'
/**
 * A scripted player: one mineflayer client the bench logs in as another person on the server, driven over stdio.
 *
 * It only talks and listens. Where it stands, what it holds and its game mode are set over RCON by the runner (it is
 * put in adventure mode, so it never breaks a block); the task's events say what it says and when. Every chat line it
 * hears, its own included, is kept for the trial's record.
 *
 * Requests are JSON lines on stdin, {id, op, ...}; each gets one JSON line on stdout, {id, ok, result} or
 * {id, ok: false, error}. Anything else this process has to say goes to stderr.
 *
 *   connect     {host, port, username, version}   -> {username}
 *   connected                                     -> bool
 *   disconnect                                    -> null
 *   say         {text}                            -> null (sent as the player's own chat)
 *   drain_heard                                   -> [[t_ms, from, text, kind]...] heard since the last drain
 *                                                    (kind: chat | whisper, a whisper being to this player alone)
 */
const readline = require('readline')
const mineflayer = require('mineflayer')

let bot = null
let alive = false
let heard = []
const HEARD_CAP = 5000

function need () {
  if (!bot || !alive) throw new Error('player is not connected')
  return bot
}

function connect ({ host, port, username, version, timeout_ms: timeoutMs }) {
  if (bot) { try { bot.quit() } catch (_) {} }
  bot = mineflayer.createBot({ host, port, username, version, auth: 'offline', hideErrors: true })
  alive = false
  heard = []
  const b = bot
  b.on('end', () => { if (bot === b) alive = false })
  b.on('chat', (from, message) => { if (heard.length < HEARD_CAP) heard.push([Date.now(), from, message, 'chat']) })
  b.on('whisper', (from, message) => { if (heard.length < HEARD_CAP) heard.push([Date.now(), from, message, 'whisper']) })
  b.on('error', (e) => process.stderr.write(`player error: ${e && e.message}\n`))
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('player: no spawn within ' + (timeoutMs || 30000) + ' ms')), timeoutMs || 30000)
    b.once('spawn', () => { clearTimeout(timer); alive = true; resolve({ username: b.username }) })
    b.once('kicked', (r) => { clearTimeout(timer); reject(new Error('kicked: ' + JSON.stringify(r))) })
    b.once('end', (r) => { clearTimeout(timer); reject(new Error('disconnected before spawn: ' + r)) })
  })
}

const OPS = {
  connect,
  connected: () => !!bot && alive,
  disconnect: () => { if (bot) { try { bot.quit() } catch (_) {} } bot = null; alive = false; return null },
  say: ({ text }) => { need().chat(String(text)); return null },
  drain_heard: () => { const out = heard; heard = []; return out }
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
