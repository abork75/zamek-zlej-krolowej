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

const imgEl          = document.getElementById('location-image');
const imgPlaceholder = document.getElementById('location-image-placeholder');
const imgLoading     = document.getElementById('image-loading');
const atmosphereEl   = document.getElementById('location-atmosphere');
const inventoryList  = document.getElementById('inventory-list');

const ITEM_ICONS = {
  'stary kij':          '🪵',
  'zardzewiały miecz':  '⚔️',
  'sakiewka ze złotem': '💰',
  'zioła lecznicze':    '🌿',
};

let currentLocation = null;

function updateStatus(state) {
  if (!state) return;
  locationEl.textContent = state.current_location_name || state.current_location;

  // Ekwipunek
  if (state.inventory.length) {
    inventoryList.innerHTML = state.inventory.map(item => `
      <div class="inv-item">
        <span class="inv-item-icon">${ITEM_ICONS[item] || '📦'}</span>
        <span>${item}</span>
      </div>`).join('');
  } else {
    inventoryList.innerHTML = '<div class="inv-empty">(pusty)</div>';
  }

  // Obrazek — tylko gdy zmieniono lokację
  if (state.current_location !== currentLocation) {
    currentLocation = state.current_location;
    loadLocationImage(state.current_location, state.atmosphere);
  }
}

function loadLocationImage(locId, atmosphere) {
  if (atmosphere) atmosphereEl.textContent = atmosphere;

  imgEl.style.display = 'none';
  imgPlaceholder.style.display = 'none';
  imgLoading.style.display = 'flex';

  const img = new Image();
  img.onload = () => {
    imgLoading.style.display = 'none';
    imgEl.src = img.src;
    imgEl.style.display = 'block';
  };
  img.onerror = () => {
    imgLoading.style.display = 'none';
    imgPlaceholder.style.display = 'flex';
  };
  img.src = `/api/location-image/${locId}?t=${Date.now()}`;
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
