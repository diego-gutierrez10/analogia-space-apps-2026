// netlify/functions/chat.mjs
import { LLM } from './persona.mjs';

const GEMINI_MODEL = 'gemini-2.5-flash';

// Rate limiting: max 10 messages per minute per IP
const ipRateLimitMap = new Map();
const RATE_WINDOW_MS = 60 * 1000;
const MAX_REQUESTS_PER_WINDOW = 10;

function isRateLimited(clientIp) {
  const now = Date.now();
  const entry = ipRateLimitMap.get(clientIp);
  if (!entry || now - entry.windowStart > RATE_WINDOW_MS) {
    ipRateLimitMap.set(clientIp, { windowStart: now, count: 1 });
    return false;
  }
  if (entry.count >= MAX_REQUESTS_PER_WINDOW) return true;
  entry.count += 1;
  return false;
}

/**
 * Equivalent to build_messages_from(history, user_message) in chat.py
 * Converts the front-end history to Gemini's 'contents' format and injects the analog context.
 */
function buildMessagesFrom(history, userMessage, analogContext, lang) {
  const contents = [];

  // Silent first context turn with the exact data of the selected site
  const contextHeader = `
CONTEXT OF THE ANALOG SITE SELECTED ON THE PLATFORM (DO NOT MODIFY OR RECOMPUTE):
- Earth coordinates: Latitude ${analogContext.latitude}, Longitude ${analogContext.longitude}
- Region / Continent: ${analogContext.country || 'Not specified'}
- Rank assigned by the algorithm: #${analogContext.global_rank || 'N/A'}
- Terrestrial morphometric and thermal variables:
  * elev_std_m: ${analogContext.elev_std_m || 'N/A'}
  * slope_p50: ${analogContext.slope_p50 || 'N/A'}
  * slope_p95: ${analogContext.slope_p95 || 'N/A'}
  * rough_p50: ${analogContext.rough_p50 || 'N/A'}
  * rough_p95: ${analogContext.rough_p95 || 'N/A'}
  * alb_median: ${analogContext.alb_median || 'N/A'}
  * lst_day_median_c: ${analogContext.lst_day_median_c || 'N/A'}
- Linked Martian site: ${analogContext.mars_name || 'Not specified'} (${analogContext.mars_coords || 'N/A'})
- Reference Martian orbital data: ${analogContext.mars_metrics || 'Not available'}
- Required language: ${lang === 'en' ? 'scientific, technical English' : 'scientific, technical Spanish'}.
`.trim();

  // Normalise front-end roles: 'bot'/'assistant' -> 'model', 'user' -> 'user'
  const validHistory = (history || []).filter((m) => m && m.content);

  if (validHistory.length === 0) {
    contents.push({
      role: 'user',
      parts: [{ text: `${contextHeader}\n\nResearcher question:\n${userMessage}` }]
    });
  } else {
    validHistory.forEach((m, idx) => {
      const role = m.role === 'bot' || m.role === 'assistant' || m.role === 'model' ? 'model' : 'user';
      const text = idx === 0 && role === 'user' ? `${contextHeader}\n\n${m.content}` : m.content;
      contents.push({ role, parts: [{ text }] });
    });

    if (userMessage) {
      contents.push({
        role: 'user',
        parts: [{ text: userMessage }]
      });
    }
  }

  return contents;
}

export default async (req) => {
  if (req.method !== 'POST') {
    return new Response(JSON.stringify({ error: 'Method not allowed' }), {
      status: 405,
      headers: { 'Content-Type': 'application/json' }
    });
  }

  const apiKey = (process.env.GEMINI_API_KEY || '').trim();
  if (!apiKey) {
    return new Response(JSON.stringify({ error: 'GEMINI_API_KEY is empty on Netlify.' }), {
      status: 500,
      headers: { 'Content-Type': 'application/json' }
    });
  }

  const clientIp =
    req.headers.get('x-nf-client-connection-ip') ||
    req.headers.get('x-forwarded-for') ||
    'unknown-ip';

  if (isRateLimited(clientIp)) {
    return new Response(
      JSON.stringify({ error: 'Message limit reached (max 10/min). Please wait a few seconds.' }),
      { status: 429, headers: { 'Content-Type': 'application/json' } }
    );
  }

  let body;
  try {
    body = await req.json();
  } catch {
    return new Response(JSON.stringify({ error: 'Invalid JSON.' }), {
      status: 400,
      headers: { 'Content-Type': 'application/json' }
    });
  }

  const { history = [], user_message = '', analogContext = null, lang = 'en' } = body;

  // Block if no terrestrial analog is selected
  const lat = Number(analogContext?.latitude);
  const lon = Number(analogContext?.longitude);
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
    return new Response(
      JSON.stringify({ error: 'Select a terrestrial analog site first.' }),
      { status: 400, headers: { 'Content-Type': 'application/json' } }
    );
  }

  const contents = buildMessagesFrom(history, user_message, analogContext, lang);

  try {
    const endpoint = `https://generativelanguage.googleapis.com/v1beta/models/${GEMINI_MODEL}:generateContent?key=${apiKey}`;

    const resp = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        systemInstruction: { parts: [{ text: LLM }] },
        contents,
        generationConfig: {
          temperature: 0.3,
          maxOutputTokens: 1100
        }
      })
    });

    const data = await resp.json();

    if (!resp.ok) {
      return new Response(
        JSON.stringify({
          error: `${data?.error?.message || `HTTP error ${resp.status}`}`
        }),
        { status: resp.status, headers: { 'Content-Type': 'application/json' } }
      );
    }

    const reply = data?.candidates?.[0]?.content?.parts?.[0]?.text;
    if (!reply) {
      return new Response(JSON.stringify({ error: 'Empty response from the model.' }), {
        status: 502,
        headers: { 'Content-Type': 'application/json' }
      });
    }

    return new Response(JSON.stringify({ reply }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' }
    });
  } catch (err) {
    return new Response(JSON.stringify({ error: `There was a problem querying the model: ${err.message}` }), {
      status: 500,
      headers: { 'Content-Type': 'application/json' }
    });
  }
};