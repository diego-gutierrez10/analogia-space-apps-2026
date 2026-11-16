import { MarsEngine } from './marsEngine.js';
import { syncChatTarget } from './geminiAnalyst.js';

let dataset = { martian_locations: [], earth_analogs: [] };
let leftHudTab = 'mars'; // 'mars' | 'earth' for the 2 tabs of the left panel
let selectedMarsId = null;
let expandedAnalogId = null;
let currentLang = 'en';
let activeHistoryEvent = null;

const I18N = {
  en: {
    brand_sub: 'Explore Martian regions and their terrestrial analogs',
    tip_info: 'Project Information',
    tip_markers: 'Toggle Location Labels',
    tip_routes: 'Toggle Rover Traverses',
    tip_history: 'Toggle Historical Pins (i)',
    tip_fullscreen: 'Fullscreen',
    tip_home: 'Global Orbital View',
    tip_zoom_in: 'Zoom In',
    tip_zoom_out: 'Zoom Out',
    tip_jezero: 'Fly to Jezero Crater (HD)',
    lbl_view_mode: 'View Mode',
    opt_natural: 'Natural',
    opt_elevation: 'Elevation (MOLA)',
    lbl_lighting: 'Lighting',
    opt_realistic: 'Realistic',
    opt_low_angle: 'Low-Angle Relief',
    opt_zenith: 'High Illumination',
    lbl_sun_angle: 'Sun Angle',
    tab_mars: 'Mars Points',
    left_tab_mars: 'Mars Site',
    left_tab_earth: 'Analogs',
    tag_rover: 'Rover · 3D Route',
    tag_lander: 'InSight Lander',
    tag_orbital: 'Orbital DTM',
    tag_hd: '2K HD Mesh',
    badge_hd: '2K HD MESH',
    badge_mola: 'MOLA 128 PPD',
    lbl_geology: 'Geology:',
    no_analogs_for_site: 'No terrestrial analogs registered for this Martian region yet.',
    open_gmaps: 'OPEN IN GOOGLE MAPS ↗',
    var_elev_std: 'Elevation Std (elev_std_m)',
    var_slope_p50: 'Median Slope (P50)',
    var_slope_p95: '95th Percentile Slope (P95)',
    var_rough_p50: 'Median Roughness (P50)',
    var_rough_p95: '95th Percentile Roughness (P95)',
    var_albedo: 'Median Albedo',
    var_lst_day: 'Median Daytime LST',
    info_title: 'About the Platform',
    info_body: 'Current phase: Martian topographic visualization (NASA MOLA 128 ppd + USGS Viking MDIM 2.1) and linking with scientifically validated terrestrial analogs.'
  },
  es: {
    brand_sub: 'Explora regiones marcianas y sus análogos terrestres',
    tip_info: 'Información del Proyecto',
    tip_markers: 'Mostrar/Ocultar Etiquetas de Ubicaciones',
    tip_routes: 'Mostrar/Ocultar Rutas de Rovers',
    tip_history: 'Mostrar/Ocultar Puntos de Historial (i)',
    tip_fullscreen: 'Pantalla Completa',
    tip_home: 'Vista Orbital Global',
    tip_zoom_in: 'Acercar',
    tip_zoom_out: 'Alejar',
    tip_jezero: 'Volar al Cráter Jezero (HD)',
    lbl_view_mode: 'Modo de Vista',
    opt_natural: 'Natural',
    opt_elevation: 'Elevación (MOLA)',
    lbl_lighting: 'Iluminación',
    opt_realistic: 'Realista',
    opt_low_angle: 'Relieve Rasante',
    opt_zenith: 'Iluminación Alta',
    lbl_sun_angle: 'Ángulo Solar',
    tab_mars: 'Puntos Marte',
    left_tab_mars: 'Sitio Marte',
    left_tab_earth: 'Análogos',
    tag_rover: 'Rover · Ruta 3D',
    tag_lander: 'Lander InSight',
    tag_orbital: 'Orbital DTM',
    tag_hd: 'Malla HD 2K',
    badge_hd: 'MALLA HD 2K',
    badge_mola: 'MOLA 128 PPD',
    lbl_geology: 'Geología:',
    no_analogs_for_site: 'Aún no hay análogos terrestres registrados para esta región marciana.',
    open_gmaps: 'ABRIR EN GOOGLE MAPS ↗',
    var_elev_std: 'Desv. Estándar Elevación',
    var_slope_p50: 'Pendiente Mediana (P50)',
    var_slope_p95: 'Pendiente Percentil 95 (P95)',
    var_rough_p50: 'Rugosidad Mediana (P50)',
    var_rough_p95: 'Rugosidad Percentil 95 (P95)',
    var_albedo: 'Albedo Mediano',
    var_lst_day: 'Temp. Superficial Diurna (LST)',
    info_title: 'Sobre la Plataforma',
    info_body: 'Fase actual: Visualización topográfica de Marte (NASA MOLA 128 ppd + USGS Viking MDIM 2.1) y vinculación con análogos terrestres validados por el equipo científico.'
  }
};

function t(key) {
  return I18N[currentLang]?.[key] || I18N.en[key] || key;
}

function locField(obj, field) {
  if (!obj) return '';
  if (currentLang === 'es' && obj[`${field}_es`]) return obj[`${field}_es`];
  if (currentLang === 'en' && obj[`${field}_en`]) return obj[`${field}_en`];
  return obj[field] || '';
}

function applyStaticTranslations() {
  document.documentElement.lang = currentLang;

  document.querySelectorAll('[data-i18n]').forEach((el) => {
    const key = el.dataset.i18n;
    if (I18N[currentLang][key]) {
      el.textContent = I18N[currentLang][key];
    }
  });

  document.querySelectorAll('[data-i18n-title]').forEach((el) => {
    const key = el.dataset.i18nTitle;
    if (I18N[currentLang][key]) {
      el.title = I18N[currentLang][key];
    }
  });

  const switchBtn = document.getElementById('lang-switch');
  if (switchBtn) {
    switchBtn.setAttribute('aria-checked', currentLang === 'es' ? 'true' : 'false');
    switchBtn.querySelectorAll('.lang-opt').forEach((opt) => {
      opt.classList.toggle('active', opt.dataset.lang === currentLang);
    });
  }
}

function setLanguage(lang) {
  currentLang = lang;
  applyStaticTranslations();

  const localizedLocs = dataset.martian_locations.map((l) => ({
    ...l,
    name: locField(l, 'name')
  }));

  renderCardsList();

  if (selectedMarsId) {
    const currentLoc = dataset.martian_locations.find((l) => l.id === selectedMarsId);
    if (currentLoc) renderLeftHudPanel(currentLoc);
  }

  if (activeHistoryEvent) {
    showHistoryPopover(activeHistoryEvent);
  }

  engine.setMarkers(localizedLocs, (loc) => {
    const raw = dataset.martian_locations.find((l) => l.id === loc.id) || loc;
    selectMarsLocation(raw, true);
  });
}

function ensureHistoryDOM() {
  if (!document.getElementById('info-pins-overlay')) {
    const pinsDiv = document.createElement('div');
    pinsDiv.id = 'info-pins-overlay';
    pinsDiv.style.cssText = 'position:fixed;inset:0;width:100%;height:100%;pointer-events:none;z-index:3;';
    document.body.appendChild(pinsDiv);
  }

  if (!document.getElementById('history-popover')) {
    const aside = document.createElement('aside');
    aside.id = 'history-popover';
    aside.className = 'glass-card history-popover hidden';
    aside.innerHTML = `
      <div class="history-popover-header">
        <div>
          <div class="history-date-badge" id="hist-date">MISSION DATE</div>
          <h4 id="hist-title">Event Title</h4>
        </div>
        <button id="btn-close-hist" class="icon-circle-btn" style="width:26px;height:26px;font-size:0.75rem;">✕</button>
      </div>
      <p id="hist-summary">Description</p>
      <div class="history-coords-foot" id="hist-coords">📍 0.00° N, 0.00° E</div>
    `;
    document.body.appendChild(aside);
  }
}

ensureHistoryDOM();

const engine = new MarsEngine(
  document.getElementById('canvas-container'),
  document.getElementById('markers-overlay')
);

async function initApp() {
  applyStaticTranslations();

  try {
    const res = await fetch(`./data/analogs_dataset.json?v=${Date.now()}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    dataset = await res.json();
  } catch (err) {
    console.error('Error loading dataset:', err);
    const container = document.getElementById('cards-list-container');
    if (container) {
      container.innerHTML = `
        <div class="empty-state-box" style="border-color:#FF7138; color:#F5A16B;">
          <strong>Error loading analogs_dataset.json:</strong><br/>${err.message}
        </div>`;
    }
    return;
  }

  closeLeftHudPanel();
  renderCardsList();

  // Draw pins, rover routes and historical events on the 3D globe
  setTimeout(() => {
    const localizedLocs = dataset.martian_locations.map((l) => ({
      ...l,
      name: locField(l, 'name')
    }));
    engine.setMarkers(localizedLocs, (loc) => {
      const raw = dataset.martian_locations.find((l) => l.id === loc.id) || loc;
      selectMarsLocation(raw, true);
    });
    engine.renderRoutesAndHistory(dataset.martian_locations, (ev, parentLoc) => {
      if (parentLoc) selectMarsLocation(parentLoc, false);
      showHistoryPopover(ev);
    });
  }, 1200);
}

function formatCoords(lat, lon) {
  if (lat === undefined || lon === undefined || lat === null || lon === null) return 'N/A';
  const latDir = lat >= 0 ? 'N' : 'S';
  const lonDir = lon >= 0 ? 'E' : 'W';
  return `${Math.abs(lat).toFixed(2)}° ${latDir}, ${Math.abs(lon).toFixed(2)}° ${lonDir}`;
}

function getLinkedEarthAnalogs(marsLoc) {
  return dataset.earth_analogs.filter(
    (a) => a.mars_location_id === marsLoc.id || (marsLoc.earth_analog_ids || []).includes(a.id)
  );
}

function closeLeftHudPanel() {
  selectedMarsId = null;
  expandedAnalogId = null;
  leftHudTab = 'mars';
  syncChatTarget(null, null, currentLang);
  const hudCard = document.getElementById('hud-current-location');
  if (hudCard) {
    hudCard.classList.add('hidden');
    hudCard.innerHTML = '';
  }
  renderCardsList();
}

function renderCardsList() {
  const container = document.getElementById('cards-list-container');
  if (!container) return;

  const countEl = document.getElementById('mars-points-count');
  if (countEl) countEl.textContent = dataset.martian_locations.length;

  container.innerHTML = '';

  dataset.martian_locations.forEach((loc) => {
    const card = document.createElement('div');
    card.className = `location-card ${loc.id === selectedMarsId ? 'selected' : ''}`;
    card.dataset.locId = loc.id;

    const tags = [];
    if (loc.rover_traverse) {
      tags.push(`<span class="tag-pill rover">${t('tag_rover')}</span>`);
    } else if (loc.id === 'elysium-planitia') {
      tags.push(`<span class="tag-pill rover">${t('tag_lander')}</span>`);
    } else {
      tags.push(`<span class="tag-pill">${t('tag_orbital')}</span>`);
    }

    if (loc.has_hd_patch) {
      tags.push(`<span class="tag-pill hd">${t('tag_hd')}</span>`);
    }

    const displayName = locField(loc, 'name');
    const fallbackBadge = loc.has_hd_patch ? 'HD 2K' : 'MOLA';
    const thumbContent = loc.image
      ? `<img src="${loc.image}" alt="${displayName}" onerror="this.style.display='none';this.parentElement.textContent='${fallbackBadge}';" />`
      : fallbackBadge;

    card.innerHTML = `
      <div class="card-thumb">${thumbContent}</div>
      <div class="card-body">
        <div class="card-title-full">${displayName}</div>
        <div class="card-coords">${formatCoords(loc.latitude, loc.longitude)} · ${loc.elevation || 'N/A'}</div>
        <div class="card-tags-row">${tags.join('')}</div>
      </div>
    `;
    card.addEventListener('click', () => selectMarsLocation(loc, true));
    container.appendChild(card);
  });
}

function buildAnalogAccordionHtml(analog) {
  const isExpanded = String(expandedAnalogId) === String(analog.id);
  const lat = Number(analog.latitude);
  const lon = Number(analog.longitude);
  const coordIdentifier = formatCoords(lat, lon);
  const rawDecimalCoords = `${lat.toFixed(5)}, ${lon.toFixed(5)}`;
  const continentCategory = analog.country || analog.continente || 'Earth';
  const rankBadge = analog.global_rank ? `#${analog.global_rank}` : (analog.similarity_score || '');

  const gmapsEmbedUrl = `https://maps.google.com/maps?q=${lat},${lon}&t=k&z=13&output=embed`;
  const gmapsDirectUrl = `https://www.google.com/maps?q=${lat},${lon}&t=k`;

  return `
    <div class="analog-accordion-item ${isExpanded ? 'expanded' : ''}" data-analog-id="${analog.id}">
      <div class="analog-item-header">
        <div class="analog-rank-badge">${rankBadge || 'EARTH'}</div>
        <div class="card-body">
          <div class="card-title-full">${coordIdentifier}</div>
          <div class="card-tags-row" style="margin-top:5px;">
            <span class="tag-pill hd">${continentCategory}</span>
          </div>
        </div>
        <span class="analog-chevron">▼</span>
      </div>

      ${isExpanded ? `
        <div class="analog-variables-drawer">
          <a href="${gmapsDirectUrl}" target="_blank" rel="noopener noreferrer" class="gmaps-preview-card" title="${t('open_gmaps')}">
            <iframe src="${gmapsEmbedUrl}" loading="lazy" referrerpolicy="no-referrer-when-downgrade"></iframe>
            <div class="gmaps-open-overlay">
              <span style="font-size:0.68rem; color:#F5F5F7; font-weight:700;">${rawDecimalCoords}</span>
              <span class="gmaps-open-pill">${t('open_gmaps')}</span>
            </div>
          </a>

          <div class="analog-vars-grid">
            <div class="analog-var-box">
              <div class="analog-var-label">${t('var_elev_std')}</div>
              <div class="analog-var-value">${analog.elev_std_m || 'N/A'}</div>
            </div>
            <div class="analog-var-box">
              <div class="analog-var-label">${t('var_lst_day')}</div>
              <div class="analog-var-value">${analog.lst_day_median_c || 'N/A'}</div>
            </div>
            <div class="analog-var-box">
              <div class="analog-var-label">${t('var_slope_p50')}</div>
              <div class="analog-var-value">${analog.slope_p50 || 'N/A'}</div>
            </div>
            <div class="analog-var-box">
              <div class="analog-var-label">${t('var_slope_p95')}</div>
              <div class="analog-var-value">${analog.slope_p95 || 'N/A'}</div>
            </div>
            <div class="analog-var-box">
              <div class="analog-var-label">${t('var_rough_p50')}</div>
              <div class="analog-var-value">${analog.rough_p50 || 'N/A'}</div>
            </div>
            <div class="analog-var-box">
              <div class="analog-var-label">${t('var_rough_p95')}</div>
              <div class="analog-var-value">${analog.rough_p95 || 'N/A'}</div>
            </div>
            <div class="analog-var-box" style="grid-column: 1 / -1;">
              <div class="analog-var-label">${t('var_albedo')}</div>
              <div class="analog-var-value">${analog.alb_median || 'N/A'}</div>
            </div>
          </div>
        </div>
      ` : ''}
    </div>
  `;
}

function renderLeftHudPanel(loc) {
  const hudCard = document.getElementById('hud-current-location');
  if (!hudCard) return;

  const linked = getLinkedEarthAnalogs(loc);
  const missionLabel = loc.rover_traverse ? ` · ${loc.rover_traverse.mission}` : '';
  const badgeText = loc.has_hd_patch ? t('badge_hd') : t('badge_mola');
  const displayName = locField(loc, 'name');
  const m = loc.mars_metrics || null;

  const metricsHtml = m
    ? `
      <div class="hud-science-details">
        <div class="control-label" style="margin-top:2px;">${t('sec_morphometry')}</div>
        <div class="analog-vars-grid">
          <div class="analog-var-box">
            <div class="analog-var-label">${t('var_elev_std')}</div>
            <div class="analog-var-value">${m.elev_std_m || 'N/A'}</div>
          </div>
          <div class="analog-var-box">
            <div class="analog-var-label">${t('var_relieve')}</div>
            <div class="analog-var-value">${m.relief_m || 'N/A'}</div>
          </div>
          <div class="analog-var-box">
            <div class="analog-var-label">${t('var_slope_p50')}</div>
            <div class="analog-var-value">${m.slope_p50 || 'N/A'}</div>
          </div>
          <div class="analog-var-box">
            <div class="analog-var-label">${t('var_slope_p95')}</div>
            <div class="analog-var-value">${m.slope_p95 || 'N/A'}</div>
          </div>
          <div class="analog-var-box">
            <div class="analog-var-label">${t('var_rough_p50')}</div>
            <div class="analog-var-value">${m.rough_p50 || 'N/A'}</div>
          </div>
          <div class="analog-var-box">
            <div class="analog-var-label">${t('var_rough_p95')}</div>
            <div class="analog-var-value">${m.rough_p95 || 'N/A'}</div>
          </div>
        </div>

        <div class="control-label" style="margin-top:6px;">${t('sec_thermal')}</div>
        <div class="analog-vars-grid">
          <div class="analog-var-box">
            <div class="analog-var-label">${t('var_ir_p10')}</div>
            <div class="analog-var-value">${m.ir_temp_p10_k || 'N/A'}</div>
          </div>
          <div class="analog-var-box">
            <div class="analog-var-label">${t('var_ir_p50')}</div>
            <div class="analog-var-value">${m.ir_temp_p50_k || 'N/A'}</div>
          </div>
          <div class="analog-var-box">
            <div class="analog-var-label">${t('var_ir_p90')}</div>
            <div class="analog-var-value">${m.ir_temp_p90_k || 'N/A'}</div>
          </div>
          <div class="analog-var-box">
            <div class="analog-var-label">${t('var_ir_std')}</div>
            <div class="analog-var-value">${m.ir_temp_std_k || 'N/A'}</div>
          </div>
        </div>

        <div class="control-label" style="margin-top:6px;">${t('sec_albedo')}</div>
        <div class="analog-vars-grid">
          <div class="analog-var-box">
            <div class="analog-var-label">${t('var_vis_p10')}</div>
            <div class="analog-var-value">${m.vis_albedo_p10 || 'N/A'}</div>
          </div>
          <div class="analog-var-box">
            <div class="analog-var-label">${t('var_vis_p50')}</div>
            <div class="analog-var-value">${m.vis_albedo_p50 || 'N/A'}</div>
          </div>
          <div class="analog-var-box">
            <div class="analog-var-label">${t('var_vis_p90')}</div>
            <div class="analog-var-value">${m.vis_albedo_p90 || 'N/A'}</div>
          </div>
          <div class="analog-var-box">
            <div class="analog-var-label">${t('var_vis_std')}</div>
            <div class="analog-var-value">${m.vis_albedo_std || 'N/A'}</div>
          </div>
        </div>
      </div>
    `
    : `<div class="empty-state-box" style="margin-top:6px;">${t('no_mars_metrics')}</div>`;

  const marsTabContent = `
    <div class="hud-hero-photo">
      <img src="${loc.image || ''}" alt="${displayName}" onerror="this.style.display='none';" />
      <div class="hud-hero-gradient"></div>
      <span class="hud-hero-badge">${badgeText}</span>
      <span class="hud-hero-credit">${loc.image_credit || 'NASA / MRO HiRISE'}</span>
    </div>

    <div>
      <div class="hud-loc-header">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="#FF7138"><path d="M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7zm0 9.5a2.5 2.5 0 0 1 0-5 2.5 2.5 0 0 1 0 5z"/></svg>
        <span>${displayName}</span>
      </div>
      <div class="hud-loc-coords">${formatCoords(loc.latitude, loc.longitude)} · ${loc.elevation || 'N/A'}${missionLabel}</div>
    </div>

    ${metricsHtml}
  `;

  const earthTabContent = linked.length > 0
    ? linked.map((analog) => buildAnalogAccordionHtml(analog)).join('')
    : `<div class="empty-state-box">${t('no_analogs_for_site')}</div>`;

  hudCard.innerHTML = `
    <div class="hud-top-bar">
      <div class="panel-tabs">
        <button class="panel-tab ${leftHudTab === 'mars' ? 'active' : ''}" data-left-tab="mars">
          <span class="tab-icon mars"></span> <span>${t('left_tab_mars')}</span>
        </button>
        <button class="panel-tab ${leftHudTab === 'earth' ? 'active' : ''}" data-left-tab="earth">
          <span class="tab-icon earth"></span> <span>${t('left_tab_earth')} (${linked.length})</span>
        </button>
      </div>
      <button id="hud-btn-close" class="hud-close-btn" title="Close">✕</button>
    </div>

    <div class="hud-tab-body">
      ${leftHudTab === 'mars' ? marsTabContent : earthTabContent}
    </div>
  `;

  hudCard.classList.remove('hidden');

  hudCard.querySelectorAll('[data-left-tab]').forEach((tabBtn) => {
    tabBtn.addEventListener('click', () => {
      leftHudTab = tabBtn.dataset.leftTab;
      renderLeftHudPanel(loc);
    });
  });

  document.getElementById('hud-btn-close')?.addEventListener('click', closeLeftHudPanel);

  hudCard.querySelectorAll('.analog-item-header').forEach((headerEl) => {
    headerEl.addEventListener('click', () => {
      const itemEl = headerEl.closest('.analog-accordion-item');
      const analogId = itemEl?.dataset.analogId;
      if (!analogId) return;

      expandedAnalogId = expandedAnalogId === analogId ? null : analogId;
      renderLeftHudPanel(loc);
    });
  });

  // Sync the grey round chat button on the right only when an analog is shown
  const selectedAnalogObj =
    leftHudTab === 'earth' && expandedAnalogId
      ? linked.find((a) => String(a.id) === String(expandedAnalogId)) || null
      : null;

  syncChatTarget(selectedAnalogObj, loc, currentLang);
}

function selectMarsLocation(loc, flyCamera = true, preserveState = false) {
  if (selectedMarsId !== loc.id && !preserveState) {
    expandedAnalogId = null;
    leftHudTab = 'mars';
  }
  selectedMarsId = loc.id;

  renderCardsList();
  renderLeftHudPanel(loc);

  const selectedCard = document.querySelector(`.location-card[data-loc-id="${loc.id}"]`);
  if (selectedCard) {
    selectedCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  if (flyCamera) {
    engine.flyToLocation(loc);
  }
}

function showHistoryPopover(ev) {
  activeHistoryEvent = ev;
  const dateEl = document.getElementById('hist-date');
  const titleEl = document.getElementById('hist-title');
  const sumEl = document.getElementById('hist-summary');
  const coordsEl = document.getElementById('hist-coords');
  const popEl = document.getElementById('history-popover');

  if (dateEl) dateEl.textContent = locField(ev, 'date');
  if (titleEl) titleEl.textContent = locField(ev, 'title');
  if (sumEl) sumEl.textContent = locField(ev, 'summary');
  if (coordsEl) coordsEl.textContent = `${formatCoords(ev.latitude, ev.longitude)}`;
  if (popEl) popEl.classList.remove('hidden');
}

document.getElementById('lang-switch')?.addEventListener('click', () => {
  setLanguage(currentLang === 'en' ? 'es' : 'en');
});

document.getElementById('btn-close-hist')?.addEventListener('click', () => {
  activeHistoryEvent = null;
  document.getElementById('history-popover')?.classList.add('hidden');
  document.querySelectorAll('.info-pin-3d').forEach((p) => p.classList.remove('active'));
});

document.getElementById('btn-cam-home')?.addEventListener('click', () => {
  closeLeftHudPanel();
  engine.flyToGlobal();
});
document.getElementById('btn-cam-zoom-in')?.addEventListener('click', () => engine.stepZoom(0.8));
document.getElementById('btn-cam-zoom-out')?.addEventListener('click', () => engine.stepZoom(1.25));
document.getElementById('btn-cam-jezero')?.addEventListener('click', () => {
  const jezero = dataset.martian_locations.find((l) => l.id === 'jezero-crater');
  if (jezero) selectMarsLocation(jezero, true);
});

document.getElementById('select-view-mode')?.addEventListener('change', (e) => engine.setViewMode(e.target.value));
document.getElementById('select-lighting')?.addEventListener('change', (e) => engine.setLightingPreset(e.target.value));

const sunSlider = document.getElementById('slider-sun-azimuth');
const sunReadout = document.getElementById('sun-angle-readout');

engine.onSunAngleUpdate = (deg, isAuto) => {
  if (sunSlider) sunSlider.value = deg;
  if (sunReadout) {
    sunReadout.textContent = isAuto ? `Auto · ${deg}°` : `${deg}° (Manual)`;
  }
};

sunSlider?.addEventListener('input', (e) => {
  const deg = parseFloat(e.target.value);
  if (sunReadout) sunReadout.textContent = `${deg}° (Manual)`;
  engine.setSunAzimuth(deg);
});

if (sunReadout && sunReadout.parentElement) {
  sunReadout.parentElement.style.cursor = 'pointer';
  sunReadout.parentElement.title = 'Click to resume automatic solar orbit / Clic para reactivar';
  sunReadout.parentElement.addEventListener('click', () => {
    engine.setAutoLight(true);
  });
}

const btnMarkers = document.getElementById('btn-markers-toggle');
btnMarkers?.addEventListener('click', () => {
  engine.showMarkers = !engine.showMarkers;
  btnMarkers.classList.toggle('active', engine.showMarkers);
});

const btnRoutes = document.getElementById('btn-routes-toggle');
btnRoutes?.addEventListener('click', () => {
  engine.showRoutes = !engine.showRoutes;
  btnRoutes.classList.toggle('active', engine.showRoutes);
});

const btnHistory = document.getElementById('btn-history-toggle');
btnHistory?.addEventListener('click', () => {
  engine.showHistory = !engine.showHistory;
  btnHistory.classList.toggle('active', engine.showHistory);
  if (!engine.showHistory) {
    activeHistoryEvent = null;
    document.getElementById('history-popover')?.classList.add('hidden');
    document.querySelectorAll('.info-pin-3d').forEach((p) => p.classList.remove('active'));
  }
});

document.getElementById('btn-info')?.addEventListener('click', () => document.getElementById('info-modal')?.classList.remove('hidden'));
document.getElementById('btn-close-info')?.addEventListener('click', () => document.getElementById('info-modal')?.classList.add('hidden'));

document.getElementById('btn-fullscreen')?.addEventListener('click', () => {
  if (!document.fullscreenElement) document.documentElement.requestFullscreen();
  else document.exitFullscreen();
});

initApp();