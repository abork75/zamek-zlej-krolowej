const messagesEl = document.getElementById('messages');
const narrativeBox = document.getElementById('narrative-box');
const textInput = document.getElementById('text-input');
const sendBtn = document.getElementById('send-btn');
const resetBtn = document.getElementById('reset-btn');
const locationEl = document.getElementById('location-name');
const inventoryEl = document.getElementById('inventory-display');
const exitsButtons = document.getElementById('exits-buttons');

function addMessage(text, type = 'gm') {
  const div = document.createElement('div');
  div.className = `message ${type}`;
  div.textContent = text;
  messagesEl.appendChild(div);
  requestAnimationFrame(() => {
    narrativeBox.scrollTop = narrativeBox.scrollHeight;
  });
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
let currentImage = null;

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

  // Przyciski wyjść
  updateExits(state);

  // Obrazek — przy zmianie lokacji LUB zmianie aktywnego wariantu (np. troll pokonany)
  const activeImage = state.active_image || state.current_location;
  if (state.current_location !== currentLocation || activeImage !== currentImage) {
    currentLocation = state.current_location;
    currentImage = activeImage;
    loadLocationImage(state.current_location, state.atmosphere);
  }
}

const DIR_ICONS = {
  'północ': '↑', 'południe': '↓', 'wschód': '→', 'zachód': '←',
  'wejście': '↪', 'wyjście': '↩', 'podejdź bliżej': '↪',
};

function updateExits(state) {
  const available = state.available_exits || [];
  const blocked = state.blocked_exits || {};
  const all = [...available, ...Object.keys(blocked)];
  exitsButtons.innerHTML = '';
  all.forEach(dir => {
    const isBlocked = !available.includes(dir);
    const icon = DIR_ICONS[dir] || '→';
    const btn = document.createElement('button');
    btn.className = 'exit-btn' + (isBlocked ? ' blocked' : '');
    btn.textContent = `${icon} ${dir}`;
    btn.disabled = isBlocked;
    if (isBlocked) btn.title = blocked[dir] || 'Zablokowane';
    else btn.addEventListener('click', () => moveDir(dir));
    exitsButtons.appendChild(btn);
  });
}

async function moveDir(direction) {
  addMessage(direction, 'player');
  sendBtn.disabled = true;
  try {
    const res = await fetch('/api/move', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ direction }),
    });
    const data = await res.json();
    if (data.narrative) addMessage(data.narrative, 'gm');
    updateStatus(data.state);
  } catch (e) {
    addMessage('(błąd połączenia)', 'gm');
  } finally {
    sendBtn.disabled = false;
    textInput.focus();
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

function showEnding(type) {
  const overlay = document.getElementById('ending-overlay');
  const video = document.getElementById('ending-video');
  const epilogue = document.getElementById('ending-epilogue');

  overlay.style.display = 'flex';
  textInput.disabled = true;
  sendBtn.disabled = true;
  exitsButtons.innerHTML = '';

  const videos = [
    '/static/img/outro/zaproszenie.mp4',
    '/static/img/outro/lubiezna_krolowa.mp4',
  ];
  let current = 0;

  function playNext() {
    if (current < videos.length) {
      video.src = videos[current++];
      video.style.display = 'block';
      epilogue.style.display = 'none';
      video.play();
    } else {
      video.style.display = 'none';
      epilogue.style.display = 'flex';
    }
  }

  video.onended = playNext;
  playNext();
}

function addThinking() {
  const div = document.createElement('div');
  div.className = 'message gm thinking';
  div.id = 'thinking-msg';
  div.textContent = '...';
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function removeThinking() {
  const el = document.getElementById('thinking-msg');
  if (el) el.remove();
}

async function sendMessage(text) {
  if (!text.trim()) return;
  addMessage(text, 'player');
  textInput.value = '';
  sendBtn.disabled = true;
  addThinking();

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text }),
    });
    const data = await res.json();
    removeThinking();
    addMessage(data.narrative, 'gm');
    updateStatus(data.state);
    if (data.ending) showEnding(data.ending);
  } catch (e) {
    removeThinking();
    addMessage('(błąd połączenia z serwerem)', 'gm');
  } finally {
    if (!document.getElementById('ending-overlay').style.display || document.getElementById('ending-overlay').style.display === 'none') {
      sendBtn.disabled = false;
    }
    textInput.focus();
  }
}

sendBtn.addEventListener('click', () => sendMessage(textInput.value));
textInput.addEventListener('keydown', e => { if (e.key === 'Enter') sendMessage(textInput.value); });

async function startNewGame(startLocation = null) {
  messagesEl.innerHTML = '';
  document.getElementById('ending-overlay').style.display = 'none';
  textInput.disabled = false;
  sendBtn.disabled = false;
  const url = startLocation ? `/api/reset?start=${startLocation}` : '/api/reset';
  const res = await fetch(url, { method: 'POST' });
  const data = await res.json();
  updateStatus(data.state);
  addMessage(data.intro, 'gm');
}

document.getElementById('reset-las-btn').addEventListener('click', () => {
  if (confirm('Zacząć nową grę w Lesie? Cały postęp zostanie utracony.')) startNewGame('las');
});
document.getElementById('reset-miasto-btn').addEventListener('click', () => {
  if (confirm('Zacząć nową grę w Mieście? Cały postęp zostanie utracony.')) startNewGame('pokoj_karczmy');
});
document.getElementById('ending-restart').addEventListener('click', () => startNewGame());

// Start — wczytaj aktualny stan bez resetowania
(async () => {
  try {
    const res = await fetch('/api/state');
    const data = await res.json();
    updateStatus(data.state);
    addMessage(data.narrative, 'gm');
  } catch {
    addMessage('Nie można połączyć się z serwerem gry. Upewnij się że backend działa.', 'gm');
  }
})();

// Eksport dla voice.js
window.addMessage = addMessage;
window.updateStatus = updateStatus;
