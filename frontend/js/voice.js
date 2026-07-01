const voiceBtn = document.getElementById('voice-btn');
const voiceStatus = document.getElementById('voice-status');

let ws = null;
let audioCtx = null;
let processor = null;
let isConnected = false;
let audioQueue = [];
let isPlaying = false;

function setVoiceState(state) {
  voiceBtn.className = `voice-${state}`;
  const labels = {
    idle: '🎤 Mów',
    listening: '🔴 Słucham...',
    active: '⏹ Rozłącz',
  };
  const statuses = {
    idle: 'Kliknij żeby mówić',
    listening: 'Mówię... kliknij żeby skończyć',
    active: 'Połączony z Mistrzem Gry',
  };
  voiceBtn.textContent = labels[state] || '🎤';
  voiceStatus.textContent = statuses[state] || '';
}

async function startVoice() {
  audioCtx = new AudioContext({ sampleRate: 24000 });
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const source = audioCtx.createMediaStreamSource(stream);

  processor = audioCtx.createScriptProcessor(4096, 1, 1);
  source.connect(processor);
  processor.connect(audioCtx.destination);

  ws = new WebSocket(`ws://${location.host}/ws/voice`);
  ws.binaryType = 'arraybuffer';

  ws.onopen = () => {
    isConnected = true;
    setVoiceState('listening');
    processor.onaudioprocess = (e) => {
      if (ws.readyState !== WebSocket.OPEN) return;
      const float32 = e.inputBuffer.getChannelData(0);
      const pcm16 = new Int16Array(float32.length);
      for (let i = 0; i < float32.length; i++) {
        pcm16[i] = Math.max(-32768, Math.min(32767, float32[i] * 32768));
      }
      ws.send(pcm16.buffer);
    };
  };

  ws.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.type === 'response.audio.delta' && msg.delta) {
        const pcm = base64ToPcm(msg.delta);
        audioQueue.push(pcm);
        if (!isPlaying) playNext();
      }
      if (msg.type === 'response.audio_transcript.done' && msg.transcript) {
        window.addMessage(msg.transcript, 'gm');
      }
      if (msg.type === 'conversation.item.input_audio_transcription.completed') {
        window.addMessage(msg.transcript, 'player');
      }
    } catch {}
  };

  ws.onclose = () => stopVoice();
}

function stopVoice() {
  isConnected = false;
  if (ws) { ws.close(); ws = null; }
  if (processor) { processor.disconnect(); processor = null; }
  if (audioCtx) { audioCtx.close(); audioCtx = null; }
  audioQueue = [];
  isPlaying = false;
  setVoiceState('idle');
}

function playNext() {
  if (!audioQueue.length || !audioCtx) { isPlaying = false; return; }
  isPlaying = true;
  const pcm = audioQueue.shift();
  const buffer = audioCtx.createBuffer(1, pcm.length, 24000);
  buffer.copyToChannel(pcm, 0);
  const src = audioCtx.createBufferSource();
  src.buffer = buffer;
  src.connect(audioCtx.destination);
  src.onended = playNext;
  src.start();
}

function base64ToPcm(b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const int16 = new Int16Array(bytes.buffer);
  const float32 = new Float32Array(int16.length);
  for (let i = 0; i < int16.length; i++) float32[i] = int16[i] / 32768;
  return float32;
}

voiceBtn.addEventListener('click', () => {
  if (isConnected) stopVoice();
  else startVoice().catch(err => {
    voiceStatus.textContent = `Błąd: ${err.message}`;
  });
});
