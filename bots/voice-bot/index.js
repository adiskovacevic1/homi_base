// voice-bot: joins a Discord voice channel, listens, answers with Claude, talks back.
//   !join            in a text channel while you are in a voice channel -> it joins you
//   !leave           it leaves
//   !voice           status
// Speech: ElevenLabs (Scribe for ears, Flash v2.5 for the mouth) when ELEVENLABS_API_KEY is set, else all local:
// faster-whisper (sidecar python) and Piper. The local engines stay as the fallback for any ElevenLabs call that fails.
// Env: DISCORD_TOKEN, ANTHROPIC_API_KEY, BOT_MODEL (claude-opus-5), WAKE_WORDS ("hey bot" — setup adds the bot's name; empty = answer everything),
//      ELEVENLABS_API_KEY, ELEVEN_VOICE_ID, ELEVEN_TTS_MODEL, ELEVEN_TTS_FORMAT, ELEVEN_STT_MODEL, ELEVEN_STT_LANG, ELEVEN_TTS=0 / ELEVEN_STT=0 to keep one side local,
//      WHISPER_MODEL, PIPER_VOICE, VOICE_SYSTEM (system prompt), MIN_UTTERANCE_MS (600)
//   node index.js --voices     list the ElevenLabs voices on the account, to pick ELEVEN_VOICE_ID
import { spawn, execFile } from "node:child_process";
import { promisify } from "node:util";
import { mkdirSync, unlinkSync, existsSync, statSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import Anthropic from "@anthropic-ai/sdk";
import prism from "prism-media";
import { Client, GatewayIntentBits, Events, ChannelType } from "discord.js";
import { joinVoiceChannel, createAudioPlayer, createAudioResource, EndBehaviorType, VoiceConnectionStatus,
         AudioPlayerStatus, entersState, getVoiceConnection, NoSubscriberBehavior } from "@discordjs/voice";

const execFileP = promisify(execFile);
const MODEL = process.env.BOT_MODEL || "claude-opus-5";
const WAKE = (process.env.WAKE_WORDS ?? "hey bot").split(",").map(s => s.trim().toLowerCase()).filter(Boolean);
const MIN_MS = Number(process.env.MIN_UTTERANCE_MS || 600);
const PIPER_MODEL = `/models/piper/${process.env.PIPER_VOICE || "en_US-lessac-medium"}.onnx`;
const TMP = "/data/tmp";
const HISTORY_TURNS = 12;
const SILENCE_MS = Number(process.env.SILENCE_MS || 1400);        // pause that ends an utterance (900 split "hey bot, ..." in two)
const ATTENTION_MS = Number(process.env.ATTENTION_MS || 20000);   // after a wake word or a reply, the same speaker needs no wake word
const AUTO_JOIN = (process.env.AUTO_JOIN ?? "general").split(",").map(s => s.trim().toLowerCase()).filter(Boolean);   // voice channels (name or id) it joins when someone arrives; empty = only on !join
const EMPTY_LEAVE_MS = Number(process.env.EMPTY_LEAVE_MS || 15000);   // how long the channel stays empty before it leaves
// "bye" or "leave" said to the bot (after the wake word or inside the attention window) sends it off; kept short so "leave the lights on" does not
const FAREWELL = /^(?:ok(?:ay)?[,.]? |thanks?[,.]? |thank you[,.]? |please |you can )?(?:bye(?: bye)?|good ?bye|see ya|see you(?: later)?|later|good ?night|go away|get out|leave(?: (?:the )?(?:channel|chat|room|call|voice))?|disconnect|hang up|log off)(?: now| please| bot| for now)?[.!]*$/i;
const DEBUG = process.env.VOICE_DEBUG === "1";
const SYSTEM = process.env.VOICE_SYSTEM || (
  "You are a voice assistant in a Discord voice channel with a group of friends. You hear transcripts of what people say " +
  "and your reply is read aloud by a text-to-speech engine, so: answer in one to three short spoken sentences, plain words, " +
  "no markdown, no lists, no URLs, no emoji, spell out numbers under ten. If a transcript is garbled or unclear, ask a brief " +
  "clarifying question rather than guessing. Be warm and quick.");
mkdirSync(TMP, { recursive: true });
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);

// ---------------------------------------------------------------- secrets: from the text bot's vault, via its internal /secrets route
// The Discord token, the model key and the ElevenLabs key live in one vault the owner edits from the console. This bot asks
// the brain for them at start (retrying until it is up), again every ten minutes, and whenever ElevenLabs rejects the key,
// so a key changed in the console applies here without a restart. Plain env vars still work as the fallback.
const BRAIN_URL = process.env.BRAIN_URL || "";
const BRAIN_BASE = BRAIN_URL.replace(/\/ask\/?$/, "");
const INTERNAL_TOKEN = process.env.INTERNAL_TOKEN || "";
const SECRET_NAMES = ["DISCORD_TOKEN", "ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY", "ELEVEN_VOICE_ID"];
const secretsNow = Object.fromEntries(SECRET_NAMES.map(n => [n, process.env[n] || ""]));
async function fetchSecrets() {
  if (!BRAIN_BASE || !INTERNAL_TOKEN) return [];
  const r = await fetch(`${BRAIN_BASE}/secrets?names=${SECRET_NAMES.join(",")}`, { headers: { "x-internal-token": INTERNAL_TOKEN }, signal: AbortSignal.timeout(5000) });
  if (!r.ok) throw new Error(`brain HTTP ${r.status}`);
  const { secrets } = await r.json(); const changed = [];
  for (const n of SECRET_NAMES) if (secrets[n] && secrets[n] !== secretsNow[n]) { secretsNow[n] = secrets[n]; changed.push(n); }
  if (changed.length) applySecrets(changed);
  return changed;
}

// ---------------------------------------------------------------- ElevenLabs (optional): with a key, speech goes through their API
let ELEVEN_KEY = "", ELEVEN_TTS = false, ELEVEN_STT = false, ELEVEN_VOICE = "21m00Tcm4TlvDq8ikWAM";   // "Rachel", a premade voice; --voices lists the account's
const ELEVEN_TTS_MODEL = process.env.ELEVEN_TTS_MODEL || "eleven_flash_v2_5";  // ~75ms model latency; eleven_multilingual_v2 for quality
const ELEVEN_TTS_FORMAT = process.env.ELEVEN_TTS_FORMAT || "mp3_44100_128";   // available on every tier; opus_48000_* also plays directly
const ELEVEN_STT_MODEL = process.env.ELEVEN_STT_MODEL || "scribe_v1";
const ELEVEN_STT_LANG = process.env.ELEVEN_STT_LANG ?? "en";                   // "" = let Scribe detect the language
const ELEVEN_TIMEOUT_MS = Number(process.env.ELEVEN_TIMEOUT_MS || 20000);
const ELEVEN_EXT = ELEVEN_TTS_FORMAT.startsWith("mp3") ? "mp3" : ELEVEN_TTS_FORMAT.startsWith("opus") ? "ogg" : ELEVEN_TTS_FORMAT.startsWith("wav") ? "wav" : null;
if (!ELEVEN_EXT) { console.error(`ELEVEN_TTS_FORMAT=${ELEVEN_TTS_FORMAT} is not playable here; use an mp3_*, opus_* or wav_* format`); process.exit(2); }
let anthropic = new Anthropic({ apiKey: secretsNow.ANTHROPIC_API_KEY || "not-set-yet" });
function applySecrets(changed) {
  ELEVEN_KEY = secretsNow.ELEVENLABS_API_KEY;
  ELEVEN_TTS = !!ELEVEN_KEY && process.env.ELEVEN_TTS !== "0";
  ELEVEN_STT = !!ELEVEN_KEY && process.env.ELEVEN_STT !== "0";
  ELEVEN_VOICE = secretsNow.ELEVEN_VOICE_ID || "21m00Tcm4TlvDq8ikWAM";
  if (changed.includes("ANTHROPIC_API_KEY")) anthropic = new Anthropic({ apiKey: secretsNow.ANTHROPIC_API_KEY || "not-set-yet" });
  if (changed.length) log(`secrets ${changed.join(", ")} applied | ${engines()}`);
}
applySecrets([]);                                                              // from env, before anything asks the brain

async function elevenFetch(url, init = {}) {
  const ctl = new AbortController(); const t = setTimeout(() => ctl.abort(), ELEVEN_TIMEOUT_MS);
  try {
    const r = await fetch(url, { ...init, signal: ctl.signal, headers: { "xi-api-key": ELEVEN_KEY, ...(init.headers || {}) } });
    if (r.status === 401) fetchSecrets().catch(() => {});                       // the key may have been changed in the console
    if (!r.ok) throw new Error(`ElevenLabs HTTP ${r.status}: ${(await r.text()).replace(/\s+/g, " ").slice(0, 200)}`);
    return r;
  } finally { clearTimeout(t); }
}
async function elevenTTS(text, outPath) {
  const r = await elevenFetch(`https://api.elevenlabs.io/v1/text-to-speech/${ELEVEN_VOICE}?output_format=${ELEVEN_TTS_FORMAT}`, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ text, model_id: ELEVEN_TTS_MODEL, voice_settings: { stability: 0.5, similarity_boost: 0.75, speed: 1.0 } }) });
  writeFileSync(outPath, Buffer.from(await r.arrayBuffer()));
  return outPath;
}
async function elevenSTT(wavPath) {
  const form = new FormData();
  form.append("model_id", ELEVEN_STT_MODEL);
  if (ELEVEN_STT_LANG) form.append("language_code", ELEVEN_STT_LANG);
  form.append("tag_audio_events", "false");
  form.append("file", new Blob([readFileSync(wavPath)], { type: "audio/wav" }), "utterance.wav");
  const j = await (await elevenFetch("https://api.elevenlabs.io/v1/speech-to-text", { method: "POST", body: form })).json();
  return { text: (j.text || "").trim(), language: j.language_code };
}
async function listVoices() {
  if (!ELEVEN_KEY) throw new Error("set ELEVENLABS_API_KEY in .env first");
  const j = await (await elevenFetch("https://api.elevenlabs.io/v1/voices")).json();
  console.log("voice_id              name                 category   labels");
  for (const v of j.voices || [])
    console.log(`${v.voice_id}  ${(v.name || "").padEnd(20)} ${(v.category || "").padEnd(10)} ${v.labels ? Object.values(v.labels).join(", ") : ""}`);
  console.log(`\ncurrent ELEVEN_VOICE_ID: ${ELEVEN_VOICE}${(j.voices || []).some(v => v.voice_id === ELEVEN_VOICE) ? "" : "  (not in this list - premade voices may still work)"}`);
}

// ---------------------------------------------------------------- ears: one long-lived whisper process (started lazily when ElevenLabs hears)
class STT {
  constructor(lazy) { this.pending = new Map(); this.ready = new Promise(r => (this._ready = r)); this.started = false; if (!lazy) this.start(); }
  start() {
    this.started = true;
    this.proc = spawn("python3", [path.join(import.meta.dirname, "stt_server.py")], { stdio: ["pipe", "pipe", "inherit"] });
    let buf = "";
    this.proc.stdout.on("data", (d) => {
      buf += d.toString();
      let i; while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1);
        if (!line) continue;
        let m; try { m = JSON.parse(line); } catch { continue; }
        if (m.ready) { log(`whisper ready (${m.model})`); this._ready(); continue; }
        const p = this.pending.get(m.path); if (!p) continue; this.pending.delete(m.path);
        m.error ? p.reject(new Error(m.error)) : p.resolve(m);
      }
    });
    this.proc.on("exit", (c) => { log("whisper sidecar exited", c, "- restarting in 3s"); for (const p of this.pending.values()) p.reject(new Error("stt restarted")); this.pending.clear(); setTimeout(() => this.start(), 3000); });
  }
  async transcribe(wavPath) {
    if (!this.started) { log("starting the whisper sidecar as fallback"); this.start(); }
    await this.ready;
    return new Promise((resolve, reject) => { this.pending.set(wavPath, { resolve, reject }); this.proc.stdin.write(wavPath + "\n"); });
  }
}
const stt = new STT(true);                                                     // started by boot() when the ears are local, else on first fallback

// ElevenLabs first when configured; the local engine covers a failed call, so a bad key or an outage costs latency, not the answer.
async function hear(wavPath) {
  if (ELEVEN_STT) {
    try { return await elevenSTT(wavPath); }
    catch (e) { log("ElevenLabs STT failed, using whisper:", e.message); }
  }
  return stt.transcribe(wavPath);
}

// ---------------------------------------------------------------- mouth: ElevenLabs -> mp3, or Piper -> wav
async function piperTTS(text, outPath) {
  await new Promise((resolve, reject) => {
    const p = spawn("piper", ["--model", PIPER_MODEL, "--output_file", outPath, "--sentence_silence", "0.25"], { stdio: ["pipe", "ignore", "pipe"] });
    let err = ""; p.stderr.on("data", d => (err += d));
    p.on("exit", c => c === 0 ? resolve() : reject(new Error(`piper exit ${c}: ${err.slice(-300)}`)));
    p.stdin.end(text);
  });
  return outPath;
}
async function speak(text, outBase) {
  if (ELEVEN_TTS) {
    try { return await elevenTTS(text, `${outBase}.${ELEVEN_EXT}`); }
    catch (e) { log("ElevenLabs TTS failed, using Piper:", e.message); }
  }
  return piperTTS(text, `${outBase}.wav`);
}
const engines = () => `stt=${ELEVEN_STT ? "elevenlabs/" + ELEVEN_STT_MODEL : "whisper/" + (process.env.WHISPER_MODEL || "base.en")} tts=${ELEVEN_TTS ? "elevenlabs/" + ELEVEN_TTS_MODEL + " voice " + ELEVEN_VOICE : "piper/" + path.basename(PIPER_MODEL, ".onnx")}`;

// ---------------------------------------------------------------- brain
// Preferred: the text bot's internal /ask endpoint (same tools, same memory). Fallback: Claude directly.
function speakable(text) {
  // strip markdown the text bot may still emit, so the TTS does not read asterisks and brackets aloud
  return text.replace(/\*\*?|__|~~|`+/g, "").replace(/\[([^\]]+)\]\([^)]*\)/g, "$1").replace(/^\s*[-*•]\s+/gm, "")
             .replace(/#+\s*/g, "").replace(/https?:\/\/\S+/g, "a link").replace(/\s+/g, " ").trim();
}
async function askBrain({ speaker, speakerId, text, key, channelId }) {
  const ctl = new AbortController(); const t = setTimeout(() => ctl.abort(), 90_000);
  try {
    // channel_id: where the brain posts a follow-up minutes later if it has a tool built for this question
    const r = await fetch(BRAIN_URL, { method: "POST", signal: ctl.signal, headers: { "content-type": "application/json", "x-internal-token": INTERNAL_TOKEN },
      body: JSON.stringify({ text, speaker, speaker_id: speakerId, key, mode: "voice", channel_id: channelId }) });
    if (!r.ok) throw new Error(`brain HTTP ${r.status}`);
    const j = await r.json();
    return { reply: speakable(j.reply || ""), notes: j.notes || [] };
  } finally { clearTimeout(t); }
}
async function think(history) {
  const res = await anthropic.beta.messages.create({
    model: MODEL, max_tokens: 600, system: SYSTEM, messages: history,
    output_config: { effort: "low" },                      // voice wants speed; adaptive thinking stays on
    betas: ["server-side-fallback-2026-07-01"], fallbacks: "default",
  });
  if (res.stop_reason === "refusal") return "I can't help with that one.";
  return res.content.filter(b => b.type === "text").map(b => b.text).join(" ").replace(/\s+/g, " ").trim() || "Sorry, I lost my train of thought.";
}

// PCM (48k stereo s16le) -> 16k mono wav for whisper
async function pcmToWav(pcm, outPath) {
  await new Promise((resolve, reject) => {
    const f = spawn("ffmpeg", ["-loglevel", "error", "-y", "-f", "s16le", "-ar", "48000", "-ac", "2", "-i", "pipe:0", "-ar", "16000", "-ac", "1", outPath]);
    let err = ""; f.stderr.on("data", d => (err += d));
    f.on("exit", c => c === 0 ? resolve() : reject(new Error("ffmpeg " + err.slice(-200))));
    f.stdin.end(pcm);
  });
}
// Whisper mishears short wake words ("hey bot" -> "hey Bart" / "hey bought" / "a bot"), so match fuzzily:
// compare the first few words of the transcript against each wake phrase with an edit-distance budget.
function editDistance(a, b) {
  const dp = Array.from({ length: a.length + 1 }, (_, i) => [i, ...Array(b.length).fill(0)]);
  for (let j = 1; j <= b.length; j++) dp[0][j] = j;
  for (let i = 1; i <= a.length; i++) for (let j = 1; j <= b.length; j++)
    dp[i][j] = Math.min(dp[i - 1][j] + 1, dp[i][j - 1] + 1, dp[i - 1][j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
  return dp[a.length][b.length];
}
function stripWake(text) {
  const words = text.toLowerCase().replace(/[^a-z0-9' ]+/g, " ").split(/\s+/).filter(Boolean);
  if (!WAKE.length) return words.length ? text : null;
  for (let start = 0; start <= Math.min(2, words.length - 1); start++) {        // allow a filler word or two first
    for (const w of WAKE) {
      const n = w.split(" ").length;
      const window = words.slice(start, start + n).join(" ");
      if (!window) continue;
      const budget = Math.max(1, Math.floor(w.replace(/ /g, "").length / 3));
      if (editDistance(window.replace(/ /g, ""), w.replace(/ /g, "")) <= budget) {
        const rest = words.slice(start + n).join(" ").trim();
        return rest || "hello";
      }
    }
  }
  return null;
}

// ---------------------------------------------------------------- one voice session per guild
const sessions = new Map();   // guildId -> { connection, player, textChannel, history, speaking:Set, queue:[] }

async function say(session, text) {
  const file = await speak(text, path.join(TMP, `say-${Date.now()}`));
  await new Promise((resolve) => {
    const resource = createAudioResource(file);              // ffmpeg probes the container, so mp3 and wav both play
    session.player.play(resource);
    const done = () => { session.player.off(AudioPlayerStatus.Idle, done); try { unlinkSync(file); } catch {} resolve(); };
    session.player.on(AudioPlayerStatus.Idle, done);
    setTimeout(done, 60_000);
  });
}

async function handleUtterance(session, userId, displayName, pcm) {
  const ms = pcm.length / (48000 * 2 * 2) * 1000;
  if (ms < MIN_MS) return;
  const wav = path.join(TMP, `u-${userId}-${Date.now()}.wav`);
  try {
    await pcmToWav(pcm, wav);
    const t0 = Date.now();
    const { text } = await hear(wav);
    if (!text) return;
    log(`heard ${displayName} (${(ms / 1000).toFixed(1)}s, stt ${Date.now() - t0}ms): ${text}`);
    const now = Date.now();
    let ask = stripWake(text);
    const attentive = (session.attention.get(userId) || 0) > now;
    if (ask === null) {
      if (!attentive) return;                                  // not talking to the bot
      ask = text.trim();                                        // follow-up inside the attention window
    }
    if (ask === "hello" && stripWake(text) === "hello" && text.trim().split(/s+/).length <= 3) {
      // just the wake word: acknowledge and wait for the actual question
      session.attention.set(userId, now + ATTENTION_MS);
      session.queue = session.queue.then(() => say(session, "Yes?")).catch(e => log("say failed:", e.message));
      return;
    }
    if (FAREWELL.test(ask.trim())) {
      log(`${displayName} said goodbye ("${ask}") - leaving`);
      session.textChannel?.send(`👋 **${displayName}:** ${ask} — leaving the voice channel.`).catch(() => {});
      session.queue = session.queue.then(() => say(session, "Bye!")).catch(() => {}).then(() => leave(session.guildId, "asked to leave"));
      return;
    }
    session.attention.set(userId, now + ATTENTION_MS);
    session.history.push({ role: "user", content: `${displayName}: ${ask}` });
    while (session.history.length > HISTORY_TURNS) session.history.shift();
    const t1 = Date.now();
    let reply, notes = [];
    if (BRAIN_URL) {
      try { ({ reply, notes } = await askBrain({ speaker: displayName, speakerId: userId, text: ask, key: `voice:${session.guildId}`, channelId: session.textChannel?.id })); }
      catch (e) { log("brain unavailable, answering locally:", e.message); }
    }
    if (!reply) reply = await think(session.history);
    session.history.push({ role: "assistant", content: reply });
    session.attention.set(userId, Date.now() + 120000);   // stay attentive while the reply is being synthesised and spoken
    log(`reply (claude ${Date.now() - t1}ms): ${reply}`);
    session.textChannel?.send(`🎙️ **${displayName}:** ${ask}\n🗣️ ${reply}${notes.length ? "\n-# " + notes.join(", ") : ""}`).catch(() => {});
    session.queue = session.queue.then(() => say(session, reply)).catch(e => log("say failed:", e.message))
      .then(() => session.attention.set(userId, Date.now() + ATTENTION_MS));   // the follow-up window starts when the bot stops talking
  } catch (e) {
    log("utterance failed:", e.message);
  } finally {
    try { unlinkSync(wav); } catch {}
  }
}

function listen(session, guild) {
  const receiver = session.connection.receiver;
  receiver.speaking.on("start", (userId) => {
    if (session.speaking.has(userId)) return;
    session.speaking.add(userId);
    const opus = receiver.subscribe(userId, { end: { behavior: EndBehaviorType.AfterSilence, duration: SILENCE_MS } });
    const decoder = new prism.opus.Decoder({ rate: 48000, channels: 2, frameSize: 960 });
    const chunks = [];
    opus.pipe(decoder);
    decoder.on("data", (c) => chunks.push(c));
    const finish = async () => {
      session.speaking.delete(userId);
      const member = guild.members.cache.get(userId) || await guild.members.fetch(userId).catch(() => null);
      handleUtterance(session, userId, member?.displayName || "someone", Buffer.concat(chunks));
    };
    decoder.once("end", finish);
    opus.once("error", (e) => { log("opus stream error", e.message); session.speaking.delete(userId); });
  });
}

const humans = (vc) => vc?.members?.filter(m => !m.user.bot).size ?? 0;
const isAutoChannel = (ch) => !!ch && AUTO_JOIN.length > 0 && ch.type === ChannelType.GuildVoice && (AUTO_JOIN.includes(ch.id) || AUTO_JOIN.includes(ch.name.toLowerCase()));

function leave(guildId, reason) {
  const s = sessions.get(guildId);
  if (!s) return false;
  clearTimeout(s.emptyTimer);
  sessions.delete(guildId);
  try { s.connection.destroy(); } catch {}
  log(`left ${s.channelName} (${reason})`);
  return true;
}

// The channel has been empty for EMPTY_LEAVE_MS: go, unless someone came back meanwhile.
function scheduleEmptyCheck(session, guild) {
  clearTimeout(session.emptyTimer);
  session.emptyTimer = setTimeout(() => {
    const vc = guild.channels.cache.get(session.channelId);
    if (sessions.get(guild.id) === session && humans(vc) === 0) {
      session.textChannel?.send(`Everyone left **${session.channelName}**, so did I.`).catch(() => {});
      leave(guild.id, "channel empty");
    }
  }, EMPTY_LEAVE_MS);
}

// Join a voice channel and post transcripts to textChannel (a text channel for !join; the voice channel's own chat when auto-joining).
async function joinChannel(guild, vc, textChannel, { greet = true } = {}) {
  const existing = sessions.get(guild.id);
  if (existing) leave(guild.id, existing.channelId === vc.id ? "rejoining" : "moving channels");
  const connection = joinVoiceChannel({ channelId: vc.id, guildId: guild.id, adapterCreator: guild.voiceAdapterCreator, selfDeaf: false, selfMute: false, debug: DEBUG });
  connection.on("stateChange", (o, n) => log(`voice state ${o.status} -> ${n.status}${n.reason !== undefined ? " reason=" + n.reason : ""}${n.closeCode !== undefined ? " close=" + n.closeCode : ""}`));
  if (DEBUG) connection.on("debug", (m) => { const t = String(m).replace(/"token":"[^"]+"/g, '"token":"…"'); if (!/heartbeat|speaking/i.test(t)) log("voice debug:", t.replace(/\s+/g, " ").slice(0, 1500)); });
  connection.receiver.speaking.on("start", () => {});
  connection.on("error", (e) => log("voice error:", e.message));
  const player = createAudioPlayer({ behaviors: { noSubscriber: NoSubscriberBehavior.Pause } });
  connection.subscribe(player);
  const session = { connection, player, textChannel, guildId: guild.id, channelId: vc.id, channelName: vc.name, history: [], speaking: new Set(), attention: new Map(), queue: Promise.resolve(), emptyTimer: null };
  sessions.set(guild.id, session);
  try {
    await entersState(connection, VoiceConnectionStatus.Ready, 20_000);
  } catch (e) {
    leave(guild.id, "could not connect");
    throw new Error(`Couldn't connect to **${vc.name}**: ${e.message}. Does the bot have Connect and Speak permission there?`);
  }
  connection.on(VoiceConnectionStatus.Disconnected, async () => {
    try { await entersState(connection, VoiceConnectionStatus.Ready, 5_000); }
    catch { if (sessions.get(guild.id) === session) leave(guild.id, "disconnected"); }
  });
  listen(session, guild);
  if (!ELEVEN_STT) await stt.ready;
  const hint = WAKE.length ? `Say "${WAKE[0]}" and then your question — after that you can just keep talking for a bit.` : "I'll answer anything I hear.";
  if (greet) session.queue = session.queue.then(() => say(session, `Hi, I'm listening. ${hint} Say bye when you want me to go.`)).catch(e => log("greeting failed:", e.message));
  log("joined", vc.name, `(${humans(vc)} there)`);
  if (humans(vc) === 0) scheduleEmptyCheck(session, guild);
  return { session, hint };
}

async function join(msg) {
  const vc = msg.member?.voice?.channel;
  if (!vc) return msg.reply("Join a voice channel first, then say `!join`.");
  try {
    const { hint } = await joinChannel(msg.guild, vc, msg.channel);
    await msg.reply(`Joined **${vc.name}**. ${hint} I'll post what I hear and say here. \`!leave\` or saying bye sends me off; I also leave when the channel empties.`);
  } catch (e) { await msg.reply(e.message); }
}

// Auto-join: someone arrives in a listed channel while the bot is idle in that server; on startup, channels already occupied.
async function autoJoin(guild, vc) {
  if (sessions.has(guild.id) || !isAutoChannel(vc) || humans(vc) === 0) return;
  try {
    await joinChannel(guild, vc, vc);                       // a voice channel has its own text chat; transcripts go there
    vc.send?.(`Joined since someone's here. Say "${WAKE[0] || "anything"}" to talk to me, "bye" to send me off; I leave when the channel empties.`).catch(() => {});
  } catch (e) { log("auto-join failed:", e.message); }
}

// ---------------------------------------------------------------- discord
async function runDiscord() {
  const client = new Client({ intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildVoiceStates, GatewayIntentBits.GuildMessages, GatewayIntentBits.MessageContent] });
  client.once(Events.ClientReady, async (c) => {
    log(`voice-bot online as ${c.user.tag} | model=${MODEL} | wake=${WAKE.join("/") || "(none)"} | brain=${BRAIN_URL || "local claude"} | ${engines()} | auto-join=${AUTO_JOIN.join("/") || "off"}`);
    for (const guild of c.guilds.cache.values())          // people already talking when the bot comes up
      for (const ch of guild.channels.cache.filter(isAutoChannel).values()) await autoJoin(guild, ch);
  });
  // Who it talks to: the text bot's TALK_TO and owner list (its /policy route), refreshed every few minutes. Owners-only
  // means only an owner can call it into a voice channel, and it only auto-joins for an owner; spoken questions from
  // others are refused by the brain itself.
  let policy = { talk_to: "owners", owners: [] };
  const refreshPolicy = async () => {
    if (!BRAIN_BASE || !INTERNAL_TOKEN) return;
    try {
      const r = await fetch(`${BRAIN_BASE}/policy`, { headers: { "x-internal-token": INTERNAL_TOKEN }, signal: AbortSignal.timeout(5000) });
      if (r.ok) policy = await r.json();
    } catch (e) { /* keep the last one; the brain may be restarting */ }
  };
  await refreshPolicy(); setInterval(refreshPolicy, 5 * 60 * 1000).unref();
  const mayTalk = (userId) => policy.talk_to === "anyone" || policy.owners.length === 0 || policy.owners.includes(String(userId));
  const toldOff = new Map();
  const ownerOnlyLine = (userId) => {
    if (Date.now() - (toldOff.get(userId) || 0) < 3600e3) return null;
    toldOff.set(userId, Date.now());
    return `I'm set up for ${policy.owners[0] ? `<@${policy.owners[0]}>` : "my owner"} only and don't answer anyone else, sorry.`;
  };

  client.on(Events.VoiceStateUpdate, async (before, after) => {
    const guild = after.guild, s = sessions.get(guild.id);
    if (after.member?.user.bot) return;
    if (!s && after.channelId && after.channelId !== before.channelId && !mayTalk(after.member?.id)) return;   // no auto-join for a non-owner
    if (s && before.channelId === s.channelId && after.channelId !== s.channelId) {          // someone left our channel
      const vc = guild.channels.cache.get(s.channelId);
      if (humans(vc) === 0) scheduleEmptyCheck(s, guild);
    }
    if (s && after.channelId === s.channelId && before.channelId !== s.channelId) clearTimeout(s.emptyTimer);   // someone came (back)
    if (!s && after.channelId && after.channelId !== before.channelId) await autoJoin(guild, after.channel);
  });
  client.on(Events.MessageCreate, async (msg) => {
    if (msg.author.bot || !msg.guild || msg.channel.type === ChannelType.DM) return;
    const cmd = msg.content.trim().toLowerCase();
    if (!["!join", "!leave", "!voice"].includes(cmd)) return;
    if (!mayTalk(msg.author.id)) {
      const line = ownerOnlyLine(msg.author.id);
      if (line) msg.reply({ content: line, allowedMentions: { parse: [] } }).catch(() => {});
      return;
    }
    try {
      if (cmd === "!join") await join(msg);
      else if (cmd === "!leave") await msg.reply(leave(msg.guild.id, "!leave") ? "Left the voice channel." : "I'm not in a voice channel.");
      else if (cmd === "!voice") { const s = sessions.get(msg.guild.id); await msg.reply((s ? `In **${s.channelName}** with ${humans(msg.guild.channels.cache.get(s.channelId))} people; ${s.history.length / 2 | 0} exchanges so far. Wake words: ${WAKE.join(", ") || "none"}.` : "Not in a voice channel. `!join` while you're in one.") + `\n-# ${engines()} | auto-join: ${AUTO_JOIN.join(", ") || "off"}`); }
    } catch (e) { log("command failed:", e); msg.reply(`Something went wrong: ${e.message}`).catch(() => {}); }
  });
  await client.login(secretsNow.DISCORD_TOKEN);
}

// ---------------------------------------------------------------- start: secrets first, then whatever was asked
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
async function boot() {
  if (BRAIN_BASE && INTERNAL_TOKEN) {
    for (let i = 0; ; i++) {
      try { await fetchSecrets(); break; }
      catch (e) { if (secretsNow.DISCORD_TOKEN || i >= 24) { log("brain not answering for secrets; using what the environment has:", e.message); break; }
                  if (i % 6 === 0) log("waiting for the text bot to hand over secrets:", e.message); await sleep(5000); }
    }
    setInterval(() => fetchSecrets().catch(() => {}), 600_000);
  }
  if (!ELEVEN_STT && !stt.started) stt.start();                                // local ears: warm whisper up now rather than on the first word
}

// ---------------------------------------------------------------- selftest: tts -> stt -> claude -> tts, no Discord
async function selftest() {
  console.log(engines());
  if (!ELEVEN_STT) await stt.ready;
  const t0 = Date.now();
  const w1 = await speak("Hey bot, what is a good name for a pool robot?", path.join(TMP, "selftest-in"));
  const t1 = Date.now();
  const { text } = await hear(w1);
  const t2 = Date.now();
  console.log(`tts ${t1 - t0}ms -> ${path.extname(w1).slice(1)} ${statSync(w1).size} bytes | stt ${t2 - t1}ms -> "${text}"`);
  const ask = stripWake(text);
  console.log(`wake word stripped -> ${ask === null ? "NO MATCH (would be ignored)" : JSON.stringify(ask)}`);
  for (const probe of ["Hey Bart, turn the lights on", "hey bought what time is it", "um, dock bot, hello", "so anyway I think the bot is fine"])
    console.log(`  probe "${probe}" -> ${JSON.stringify(stripWake(probe))}`);
  for (const f of ["bye", "okay bye bot", "thanks, see you later", "go away", "leave the channel please", "leave the lights on", "goodbye to all that nonsense", "disconnect"])
    console.log(`  farewell "${f}" -> ${FAREWELL.test(f) ? "LEAVES" : "stays"}`);
  let reply;
  if (BRAIN_URL) {
    try { const r = await askBrain({ speaker: "Adis", speakerId: "0", text: ask ?? text, key: "voice:selftest" }); reply = r.reply; console.log("brain (DockerBot) answered; notes:", r.notes); }
    catch (e) { console.log("brain failed:", e.message, "- falling back to local claude"); }
  }
  if (!reply) reply = await think([{ role: "user", content: `Adis: ${ask ?? text}` }]);
  const t3 = Date.now();
  console.log(`claude ${t3 - t2}ms -> "${reply}"`);
  const w2 = await speak(reply, path.join(TMP, "selftest-out"));
  console.log(`tts ${Date.now() - t3}ms -> ${statSync(w2).size} bytes | total ${Date.now() - t0}ms`);
  stt.proc?.kill(); process.exit(0);
}

if (process.argv.includes("--voices")) boot().then(listVoices).then(() => { stt.proc?.kill(); process.exit(0); }).catch(e => { console.error(e.message); stt.proc?.kill(); process.exit(1); });
else if (process.argv.includes("--selftest")) boot().then(selftest).catch(e => { console.error("selftest failed:", e); process.exit(1); });
else boot().then(() => {
  if (!secretsNow.DISCORD_TOKEN) { log("no DISCORD_TOKEN from the vault or the environment — idling; add it in the console and restart"); setInterval(() => {}, 1 << 30); return; }
  return runDiscord();
}).catch(e => { log("fatal:", e); process.exit(1); });
