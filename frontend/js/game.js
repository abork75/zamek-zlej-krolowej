const messagesEl = document.getElementById('messages');
const textInput = document.getElementById('text-input');
const sendBtn = document.getElementById('send-btn');
const resetBtn = document.getElementById('reset-btn');
const locationEl = document.getElementById('location-name');
const inventoryEl = document.getElementById('inventory-display');

function addMessage(text, type = 'gm') {
  const div = document.createElement('div');
  div.className = `message ${type}`;
  div.textContent = text;
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function updateStatus(state) {
  if (!state) return;
  const loc = {
    las: 'Mroczny Las', polana: 'Słoneczna Polana',
    most: 'Most nad Rzeką Mgieł', zamek: 'Brama Zamku'
  };
  locationEl.textContent = loc[state.current_location] || state.current_location;
  const inv = state.inventory.length ? state.inventory.join(', ') : '(pusty)';
  inventoryEl.textContent = `Ekwipunek: ${inv}`;
}

async function sendMessage(text) {
  if (!text.trim()) return;
  addMessage(text, 'player');
  textInput.value = '';
  sendBtn.disabled = true;

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text }),
    });
    const data = await res.json();
    addMessage(data.narrative, 'gm');
    updateStatus(data.state);
  } catch (e) {
    addMessage('(błąd połączenia z serwerem)', 'gm');
  } finally {
    sendBtn.disabled = false;
    textInput.focus();
  }
}

sendBtn.addEventListener('click', () => sendMessage(textInput.value));
textInput.addEventListener('keydown', e => { if (e.key === 'Enter') sendMessage(textInput.value); });

resetBtn.addEventListener('click', async () => {
  messagesEl.innerHTML = '';
  const res = await fetch('/api/reset', { method: 'POST' });
  const data = await res.json();
  updateStatus(data.state);
  addMessage(data.intro, 'gm');
});

// Start — załaduj intro
(async () => {
  try {
    const res = await fetch('/api/reset', { method: 'POST' });
    const data = await res.json();
    updateStatus(data.state);
    addMessage(data.intro, 'gm');
  } catch {
    addMessage('Nie można połączyć się z serwerem gry. Upewnij się że backend działa.', 'gm');
  }
})();

// Eksport dla voice.js
window.addMessage = addMessage;
window.updateStatus = updateStatus;
