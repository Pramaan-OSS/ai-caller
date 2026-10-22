const main = document.querySelector("#main");
const next = document.querySelector("#next");
const view = document.querySelector("#view");
const tabs = document.querySelectorAll("[data-tab]");
const END = "we_0123456789abcdef0123456789abcdef";

const pinFor = {
  completed: "Done",
  finished: "Done",
  active: "Done",
  in_progress: "Working",
  running: "Working",
  dialing: "Working",
  queued: "Waiting",
  paused: "Paused",
  no_answer: "Paused",
  busy: "Paused",
  cancelled: "Blocked",
  blocked: "Blocked",
  ready: "Done",
  processing: "Working",
  failed: "Failed",
};

function crumb(section) {
  return `<p class="crumb"><a href="#agents">AI Caller</a> / ${section}</p>`;
}
const openCallStatus = new Set(["in_progress", "dialing", "queued", "ringing"]);
function pin(status) {
  const label = pinFor[status] || "Waiting";
  return `<span class="pin" data-pin="${label}">${label}</span>`;
}
function endControl(ref, status, retryable) {
  if (openCallStatus.has(status)) {
    return `<button class="quiet" type="button" data-end="${escapeHtml(ref)}">End call</button>`;
  }
  if (status === "failed") {
    const again = retryable === false ? "" : `<p class="reason">This call failed. You can place it again.</p>`;
    return `<button class="quiet" type="button" disabled aria-disabled="true">End call</button>
    ${again}`;
  }
  return `<button class="quiet" type="button" disabled aria-disabled="true">End call</button>
    <p class="reason">This call has already ended.</p>`;
}
async function endCall(ref) {
  const yes = window.confirm("This ends the call. The person on the line will be disconnected.");
  if (!yes) return false;
  await api("/api/calls/" + encodeURIComponent(ref) + "/end", {confirm: true});
  return true;
}
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"]/g, (ch) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[ch]));
}
async function api(path, body, method) {
  const response = await fetch(path, {
    method: method || (body ? "POST" : "GET"),
    headers: body ? {"Content-Type": "application/json"} : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const kind = response.headers.get("Content-Type") || "";
  const payload = kind.includes("json") ? await response.json().catch(() => ({})) : null;
  if (!response.ok) throw new Error((payload && payload.error) || "That did not work.");
  return payload;
}
function threadOff() { main.setAttribute("data-thread-none", ""); }
function threadOn() { main.removeAttribute("data-thread-none"); }
function callerSelect(id, items) {
  const options = [`<option value="">Typed number</option>`].concat(
    (items || []).map((item) => `<option value="${escapeHtml(item.number)}">${escapeHtml(item.number)}</option>`)
  ).join("");
  return `<label for="${id}">Imported caller ID</label><select id="${id}">${options}</select>`;
}

function bindCallerPick(selectId, inputId) {
  const select = document.querySelector("#" + selectId);
  if (!select) return;
  select.addEventListener("change", () => {
    if (select.value) document.querySelector("#" + inputId).value = select.value;
  });
}

function err(id, error) {
  const node = document.querySelector(id);
  if (node) node.textContent = error.message;
}

function setTab(name) {
  tabs.forEach((button) => {
    if (button.dataset.tab === name) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  const screens = {
    agents: showAgents,
    tools: showTools,
    knowledge: showKnowledge,
    numbers: showNumbers,
    calls: showCalls,
    campaigns: showCampaigns,
    optouts: showOptOuts,
    settings: showSettings,
  };
  screens[name]();
}
tabs.forEach((button) => button.addEventListener("click", () => setTab(button.dataset.tab)));

document.querySelector("#theme").addEventListener("click", () => {
  const dark = document.documentElement.getAttribute("data-theme") === "dark";
  document.documentElement.setAttribute("data-theme", dark ? "light" : "dark");
  document.querySelector("#theme").textContent = dark ? "Dark" : "Light";
});

function voiceChoices(current) {
  const keys = [
    ["", "Standard"],
    ["default", "Default"],
    ["female-1", "Female 1"],
    ["female-2", "Female 2"],
    ["female-3", "Female 3"],
    ["male-1", "Male 1"],
    ["male-2", "Male 2"],
    ["male-3", "Male 3"],
  ];
  const options = keys.map(([value, label]) => `<option value="${value}">${label}</option>`);
  if (current && !voiceKnown(current)) {
    options.push(`<option value="${escapeHtml(current)}">No longer offered</option>`);
  }
  return options.join("");
}

function voiceKnown(value) {
  return ["default", "female-1", "female-2", "female-3", "male-1", "male-2", "male-3"].includes(value);
}

function voiceLine(value) {
  const labels = {
    default: "Default",
    "female-1": "Female 1",
    "female-2": "Female 2",
    "female-3": "Female 3",
    "male-1": "Male 1",
    "male-2": "Male 2",
    "male-3": "Male 3",
  };
  if (!value) return `<p class="meta">Voice standard</p>`;
  if (labels[value]) return `<p class="meta">Voice ${labels[value]}</p>`;
  return `<p class="meta">This voice is no longer offered. The agent still runs. Pick a listed voice to change it.</p>`;
}

function toolPayload(data) {
  const mode = data.get("mode");
  const name = (data.get("tool") || "").trim();
  if (!name) return [];
  if (mode === "code") return [{name, description: "Hosted code", code: data.get("code") || "return 1"}];
  if (mode === "endpoint") return [{name, description: "App endpoint", endpoint: data.get("endpoint")}];
  return [];
}

async function showAgents() {
  threadOn();
  let bases = {items: []};
  let knowledgeProblem = "";
  try {
    bases = await api("/api/knowledge");
  } catch (error) {
    knowledgeProblem = error.message || "";
  }
  const kbOptions = [`<option value="">None</option>`].concat(
    (bases.items || []).filter((item) => item.status === "ready").map(
      (item) => `<option value="${escapeHtml(item.ref)}">${escapeHtml(item.name)}</option>`
    )
  ).join("");
  next.innerHTML = `${crumb("Agents")}
    <p class="eyebrow">Next step</p>
    <h1>Save an agent</h1>
    <form id="agent-form">
      <label for="name">Name</label>
      <input id="name" name="name" required value="reception">
      <label for="prompt">Prompt</label>
      <textarea id="prompt" name="prompt" required>You answer the phone. Be brief. Do not invent facts.</textarea>
      <label for="greeting">First message</label>
      <input id="greeting" name="greeting" value="Hello, how can I help?">
      <label for="language">Language</label>
      <select id="language" name="language">
        <option>en</option><option>es</option><option>fr</option><option>de</option>
        <option>it</option><option>pt</option><option>hi</option><option>ja</option><option>zh</option>
      </select>
      <label for="voice">Voice</label>
      <select id="voice" name="voice">${voiceChoices("")}</select>
      <p class="meta" id="voice-note"></p>
      <label for="duration">Duration limit, seconds</label>
      <input id="duration" name="duration" type="number" min="10" max="600" value="120">
      <label for="squads">Squad membership</label>
      <input id="squads" name="squads" placeholder="front">
      <label for="tool">Tool name</label>
      <input id="tool" name="tool" value="">
      <label for="mode">Where the tool runs</label>
      <select id="mode" name="mode">
        <option value="">No tool</option>
        <option value="code">Hosted code</option>
        <option value="endpoint">App endpoint</option>
      </select>
      <label for="code">Hosted code</label>
      <textarea id="code" name="code"></textarea>
      <label for="endpoint">Endpoint id</label>
      <input id="endpoint" name="endpoint" value="">
      <label for="kb">Knowledge base</label>
      <select id="kb" name="kb">${kbOptions}</select>
      <p class="meta">A call lasts 10 to 600 seconds. The default is 120. Choose hosted code or one endpoint id. Attach a ready document base, or leave it empty.</p>
      <button class="btn-thread" data-thread type="submit">Save agent</button>
      <p class="err" id="agent-err">${escapeHtml(knowledgeProblem)}</p>
    </form>`;
  document.querySelector("#agent-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(event.target);
    const squads = String(data.get("squads") || "").split(",").map((part) => part.trim()).filter(Boolean);
    const payload = {
      name: data.get("name"),
      prompt: data.get("prompt"),
      greeting: data.get("greeting"),
      language: data.get("language"),
      maxDurationSeconds: Number(data.get("duration") || 120),
      tools: toolPayload(data),
      squads,
      knowledgeBaseRef: data.get("kb") || undefined,
      voice: data.get("voice") || undefined,
    };
    try {
      if (event.target.dataset.mode === "edit") {
        await api("/api/agents/" + data.get("name"), {
          prompt: data.get("prompt"),
          greeting: data.get("greeting"),
          language: data.get("language"),
          maxDurationSeconds: Number(data.get("duration") || 120),
          squads,
          knowledgeBaseRef: data.get("kb") || undefined,
          voice: data.get("voice") || undefined,
        }, "PUT");
      } else {
        await api("/api/agents", payload);
        for (const name of squads) {
          await api("/api/squads", {name, members: [String(data.get("name"))]});
        }
      }
      showAgents();
    } catch (error) {
      err("#agent-err", error);
    }
  });
  const listed = await api("/api/agents");
  view.innerHTML = `<h1>Agents</h1>` + (listed.items.length
    ? listed.items.map((item) => `<article class="card"><h2>${escapeHtml(item.name)}</h2>
        <p class="meta">${escapeHtml(item.language || "en")} · ${escapeHtml(item.maxDurationSeconds || 120)} seconds</p>
        <p class="meta">${escapeHtml(item.prompt || "")}</p>
        <p class="meta">${escapeHtml(item.greeting || "")}</p>
        <p class="meta">Squads: ${escapeHtml((item.squads || []).join(", ") || "none")}</p>
        <p class="meta">Knowledge ${escapeHtml(item.knowledgeName || "none")}</p>
        ${voiceLine(item.voice)}
        <button class="quiet" type="button" data-edit="${escapeHtml(item.name)}">Edit</button>
        <button class="quiet" type="button" data-del="${escapeHtml(item.name)}">Delete</button></article>`).join("")
    : `<p class="empty">No agents yet.</p>`);
  view.querySelectorAll("[data-del]").forEach((button) => button.addEventListener("click", async () => {
    await api("/api/agents/" + button.dataset.del, null, "DELETE");
    showAgents();
  }));
  view.querySelectorAll("[data-edit]").forEach((button) => button.addEventListener("click", async () => {
    const item = listed.items.find((row) => row.name === button.dataset.edit);
    document.querySelector("#name").value = item.name;
    document.querySelector("#prompt").value = item.prompt || "";
    document.querySelector("#greeting").value = item.greeting || "";
    document.querySelector("#language").value = item.language || "en";
    const voice = document.querySelector("#voice");
    if (item.voice && !Array.from(voice.options).some((option) => option.value === item.voice)) {
      voice.insertAdjacentHTML("beforeend", `<option value="${escapeHtml(item.voice)}">No longer offered</option>`);
    }
    voice.value = item.voice || "";
    document.querySelector("#voice-note").textContent = item.voice && !voiceKnown(item.voice)
      ? "This voice is no longer offered. The agent still runs. Pick a listed voice to change it."
      : "";
    document.querySelector("#duration").value = item.maxDurationSeconds || 120;
    document.querySelector("#kb").value = item.knowledgeBaseRef || "";
    document.querySelector("#agent-form").dataset.mode = "edit";
  }));
}

async function showTools() {
  threadOn();
  next.innerHTML = `${crumb("Tools")}
    <p class="eyebrow">Next step</p>
    <h1>Save a tool</h1>
    <form id="tool-form">
      <label for="tname">Name</label>
      <input id="tname" name="name" required value="lookup">
      <label for="tdesc">Description</label>
      <input id="tdesc" name="description" value="Look up an account.">
      <label for="tmode">Where it runs</label>
      <select id="tmode" name="mode"><option value="code">Hosted code</option><option value="endpoint">App endpoint</option></select>
      <label for="tcode">Hosted code</label>
      <textarea id="tcode" name="code">return 1</textarea>
      <label for="tend">Endpoint id</label>
      <input id="tend" name="endpoint" value="${END}">
      <label for="pname">Argument name</label>
      <input id="pname" name="pname" value="account">
      <label for="ptype">Argument type</label>
      <select id="ptype" name="ptype"><option>string</option><option>number</option><option>boolean</option></select>
      <label for="timeout">Timeout, seconds</label>
      <input id="timeout" name="timeout" type="number" min="1" max="30" value="10">
      <button class="btn-thread" data-thread type="submit">Save tool</button>
      <button class="quiet" id="test-tool" type="button">Test against the fake</button>
      <p class="meta" id="tool-note"></p>
      <p class="err" id="tool-err"></p>
    </form>`;
  document.querySelector("#tool-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(event.target);
    const mode = data.get("mode");
    const payload = {
      name: data.get("name"),
      description: data.get("description"),
      timeoutSeconds: Number(data.get("timeout") || 10),
      parameters: [{name: data.get("pname"), type: data.get("ptype"), description: "", required: false}],
    };
    if (mode === "code") payload.code = data.get("code");
    else payload.endpoint = data.get("endpoint");
    try {
      await api("/api/tools", payload);
      showTools();
    } catch (error) {
      err("#tool-err", error);
    }
  });
  document.querySelector("#test-tool").addEventListener("click", async () => {
    try {
      const result = await api("/api/tools/test", {name: document.querySelector("#tname").value, arguments: {account: "A1"}});
      document.querySelector("#tool-note").textContent = result.result;
    } catch (error) {
      err("#tool-err", error);
    }
  });
  const listed = await api("/api/tools");
  view.innerHTML = `<h1>Tools</h1>` + (listed.items.length
    ? listed.items.map((item) => `<article class="card">${pin("active")}<h2>${escapeHtml(item.name)}</h2><p class="meta">${escapeHtml(item.kind)} · ${escapeHtml(item.timeoutSeconds)} seconds</p>
        <button class="quiet" type="button" data-tool-del="${escapeHtml(item.name)}">Delete</button></article>`).join("")
    : `<p class="empty">No tools yet.</p>`);
  view.querySelectorAll("[data-tool-del]").forEach((button) => button.addEventListener("click", async () => {
    await api("/api/tools/" + button.dataset.toolDel, null, "DELETE");
    showTools();
  }));
}

async function showNumbers() {
  threadOn();
  next.innerHTML = `${crumb("Numbers")}
    <p class="eyebrow">Next step</p>
    <h1>Find a number</h1>
    <form id="number-form">
      <label for="area">Area code</label>
      <input id="area" name="area" value="415">
      <button class="btn-thread" data-thread type="submit">Search numbers</button>
      <button class="quiet" id="included" type="button">Use an included number</button>
      <label for="caller-number">Rented number</label>
      <input id="caller-number" name="caller" placeholder="+14155550198">
      <label for="caller-name">Display name</label>
      <input id="caller-name" name="callerName" maxlength="40">
      <label for="caller-agent">Agent for incoming calls</label>
      <input id="caller-agent" name="callerAgent" placeholder="Optional">
      <button class="quiet" id="import-caller" type="button">Use this number as caller ID for agent calls</button>
      <button class="quiet" id="disconnect-caller" type="button">Disconnect this caller ID</button>
      <div id="caller-box"></div>
      <p class="meta" id="caller-note"></p>
      <p class="err" id="caller-err"></p>
      <p class="err" id="number-err"></p>
      <div id="buy-box"></div>
    </form>
    <div class="state">
      <span class="pin" data-pin="Locked">Locked</span>
      <div><b>Inbound attach</b><p>Inbound attach is not yet live.</p>
        <button class="quiet" type="button" disabled aria-disabled="true">Attach an agent</button>
        <p class="reason">Inbound billing is not enabled.</p></div>
    </div>`;
  document.querySelector("#import-caller").addEventListener("click", () => {
    document.querySelector("#caller-err").textContent = "";
    document.querySelector("#caller-note").textContent = "Nothing changes until you confirm. The number stays rented.";
    document.querySelector("#caller-box").innerHTML = `<button class="quiet" id="import-confirm" type="button">Use this caller ID</button>`;
    document.querySelector("#import-confirm").addEventListener("click", async () => {
      try {
        await api("/api/numbers/callers", {
          number: document.querySelector("#caller-number").value,
          name: document.querySelector("#caller-name").value || undefined,
          assistant: document.querySelector("#caller-agent").value || undefined,
          confirm: true,
        });
        document.querySelector("#caller-note").textContent = "This number can place agent calls. The rental stays as it was.";
        document.querySelector("#caller-box").innerHTML = "";
        await paintNumbers();
      } catch (error) {
        err("#caller-err", error);
      }
    });
  });
  document.querySelector("#disconnect-caller").addEventListener("click", () => {
    document.querySelector("#caller-err").textContent = "";
    document.querySelector("#caller-note").textContent = "Agent calls will stop using this number. The rental stays.";
    document.querySelector("#caller-box").innerHTML = `<button class="quiet" id="disconnect-confirm" type="button">Confirm disconnect</button>`;
    document.querySelector("#disconnect-confirm").addEventListener("click", async () => {
      try {
        await api("/api/numbers/callers/disconnect", {
          number: document.querySelector("#caller-number").value,
          confirm: true,
        });
        document.querySelector("#caller-note").textContent = "Agent calls no longer use this number. The rental stays.";
        document.querySelector("#caller-box").innerHTML = "";
        await paintNumbers();
      } catch (error) {
        err("#caller-err", error);
      }
    });
  });
  document.querySelector("#included").addEventListener("click", async () => {
    try {
      await api("/api/numbers/included", {areaCode: document.querySelector("#area").value});
      await paintNumbers();
    } catch (error) {
      err("#number-err", error);
    }
  });
  document.querySelector("#number-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const found = await api("/api/numbers/search", {country: "US", areaCode: document.querySelector("#area").value});
      const number = (found.results && found.results[0] && found.results[0].phoneNumber) || "";
      let estimate = await api("/api/numbers/estimate", {country: "US", phoneNumber: number, areaCode: document.querySelector("#area").value});
      document.querySelector("[data-thread]").remove();
      document.querySelector("#buy-box").innerHTML = `${estimatePriceHtml(estimate, escapeHtml)}
        <p id="purchase-plan">Plan: ${escapeHtml(estimate.plan || "Capability purchase")}. Approve within 15 minutes.</p>
        <button class="btn-thread" data-thread id="buy" type="button">Buy this number</button>`;
      document.querySelector("#buy").addEventListener("click", async () => {
        try {
          const result = await api("/api/numbers/purchase", {confirm: true, estimate});
          if (result.approvalRequired) {
            estimate = result.estimate;
            document.querySelector("#purchase-plan").textContent = `Review the number and charges above. Plan: ${estimate.plan}. Approve within 15 minutes.`;
            document.querySelector("#buy").textContent = "Approve plan and buy this number";
            return;
          }
          showNumbers();
          await paintNumbers();
        } catch (error) {
          err("#number-err", error);
        }
      });
      await paintNumbers(found.results || []);
    } catch (error) {
      err("#number-err", error);
    }
  });
  try {
    await paintNumbers();
  } catch (error) {
    err("#number-err", error);
    view.innerHTML = `<h1>Numbers</h1><p class="empty">No numbers yet.</p>`;
  }
}
async function paintNumbers(extra) {
  const listed = await api("/api/numbers");
  const callers = await api("/api/numbers/callers");
  const rows = listed.items.concat(extra || []);
  const callerCards = (callers.items || []).map((item) => `<article class="card"><h2>${escapeHtml(item.number)}</h2><p class="meta">Caller ID</p></article>`).join("");
  const note = listed.includedNote ? `<p class="meta">Included numbers: ${escapeHtml(listed.includedNote)}</p>` : "";
  view.innerHTML = `<h1>Numbers</h1><p class="meta">Included numbers take calls. A rented number can be a caller ID after you confirm. Renting a number needs confirmation.</p>` + note +
    callerCards +
    (rows.length ? rows.map((item) => `<article class="card"><h2>${escapeHtml(item.phoneNumber || item.number)}</h2></article>`).join("") : (callerCards || note ? "" : `<p class="empty">No numbers yet.</p>`));
}

async function showCalls() {
  threadOn();
  const callers = await api("/api/numbers/callers");
  next.innerHTML = `${crumb("Calls")}
    <p class="eyebrow">Next step</p>
    <h1>Place a call</h1>
    <form id="call-form">
      <label for="to">To</label><input id="to" name="to" value="+14155550101">
      ${callerSelect("caller-pick", callers.items)}
      <label for="from">From</label><input id="from" name="from" value="+14155550100">
      <label for="who">Agent or squad</label><input id="who" name="who" value="reception">
      <label for="kind">Which one</label>
      <select id="kind" name="kind"><option value="assistant">Agent</option><option value="squad">Squad</option></select>
      <label for="vars">Variables</label><input id="vars" name="vars" placeholder="name=Ada">
      <label for="when">Schedule</label><input id="when" name="when" placeholder="">
      <label for="seconds">Duration limit, seconds</label>
      <input id="seconds" name="seconds" type="number" min="10" max="600" value="120">
      <p class="meta">A call lasts 10 to 600 seconds. The default is 120.</p>
      <button class="btn-thread" data-thread type="submit">Review this call</button>
      <div id="confirm-box"></div>
      <p class="err" id="call-err"></p>
    </form>`;
  bindCallerPick("caller-pick", "from");
  document.querySelector("#call-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(event.target);
    const variables = {};
    String(data.get("vars") || "").split(",").forEach((part) => {
      const [key, value] = part.split("=");
      if (key && value) variables[key.trim()] = value.trim();
    });
    const payload = {
      to: data.get("to"), from: data.get("from"),
      maxDurationSeconds: Number(data.get("seconds") || 120),
      variables, scheduleAt: data.get("when") || undefined,
    };
    payload[data.get("kind")] = data.get("who");
    try {
      const estimate = await api("/api/calls/estimate", payload);
      const box = document.querySelector("#confirm-box");
      document.querySelector("[data-thread]").remove();
      box.innerHTML = `${estimatePriceHtml(estimate, escapeHtml)}
        <button class="btn-thread" data-thread id="place" type="button">Place the call</button>`;
      document.querySelector("#place").addEventListener("click", async () => {
        try {
          await api("/api/calls", {
            ...payload, confirm: true, plan: estimate.plan, outcomeBody: estimate.body,
          });
          showCalls();
        } catch (error) {
          err("#call-err", error);
        }
      });
    } catch (error) {
      err("#call-err", error);
    }
  });
  try {
    await paintCalls();
  } catch (error) {
    err("#call-err", error);
    view.innerHTML = `<h1>Calls</h1><p class="empty">No calls yet.</p>`;
  }
}
async function paintCalls(status) {
  const chosen = status ?? "";
  const listed = await api("/api/calls" + (chosen ? "?status=" + encodeURIComponent(chosen) : ""));
  view.innerHTML = `<h1>Calls</h1>
    <label for="call-status">Status</label>
    <select id="call-status">
      <option value="">All</option>
      <option value="in_progress">In progress</option>
      <option value="completed">Done</option>
      <option value="cancelled">Cancelled</option>
      <option value="failed">Failed</option>
    </select>` + (listed.items.length
    ? listed.items.map((item) => `<article class="card">${pin(item.status)}<h2>${escapeHtml(item.ref)}</h2>
        <p class="meta">${escapeHtml(item.failureReason || item.transcript || "")}</p>
        ${item.summaryIncomplete ? `<p class="reason">${escapeHtml(item.summary)}</p><button class="quiet" type="button" data-summary="${escapeHtml(item.ref)}">Try the summary again</button>` : ""}
        ${endControl(item.ref, item.status, item.retryable)}
        <button class="quiet" type="button" data-open="${escapeHtml(item.ref)}">Open</button></article>`).join("")
    : `<p class="empty">No calls yet.</p>`);
  const select = view.querySelector("#call-status");
  select.value = chosen;
  select.addEventListener("change", () => paintCalls(select.value));
  view.querySelectorAll("[data-open]").forEach((button) => button.addEventListener("click", () => openCall(button.dataset.open)));
  view.querySelectorAll("[data-summary]").forEach((button) => button.addEventListener("click", async () => {
    try {
      await api("/api/calls/" + encodeURIComponent(button.dataset.summary) + "/summary", {});
      paintCalls(chosen);
    } catch (error) {
      button.insertAdjacentHTML("afterend", `<p class="err">${escapeHtml(error.message)}</p>`);
    }
  }));
  view.querySelectorAll("[data-end]").forEach((button) => button.addEventListener("click", async () => {
    if (await endCall(button.dataset.end)) paintCalls(chosen);
  }));
}
async function openCall(ref) {
  let detail;
  try {
    detail = await api("/api/calls/" + ref);
  } catch (error) {
    view.innerHTML = `<h1>Call</h1><p class="err">${escapeHtml(error.message)}</p>`;
    return;
  }
  const legs = (detail.legs || []).map((leg) => `<li>Leg ${escapeHtml(leg.position)} · ${escapeHtml(leg.status)}</li>`).join("");
  const timeline = (detail.timeline || []).map((item) => `<li>${escapeHtml(item.sentence)}</li>`).join("");
  view.innerHTML = `<h1>Call</h1><article class="card">${pin(detail.status)}
    <h2>${escapeHtml(detail.ref)}</h2>
    <p class="meta">${escapeHtml(detail.failureReason || detail.summary || "")}</p>
    <p class="meta">${escapeHtml(detail.transcript || "")}</p>
    ${detail.summaryIncomplete ? `<button class="quiet" type="button" data-summary="${escapeHtml(detail.ref)}">Try the summary again</button><p class="err" id="summary-err"></p>` : ""}
    ${endControl(detail.ref, detail.status, detail.retryable)}
    <audio controls src="/api/calls/${escapeHtml(detail.ref)}/recording"></audio>
    <ul>${timeline}</ul><ul>${legs}</ul></article>`;
  const end = view.querySelector("[data-end]");
  if (end) end.addEventListener("click", async () => {
    if (await endCall(end.dataset.end)) openCall(ref);
  });
  const again = view.querySelector("[data-summary]");
  if (again) again.addEventListener("click", async () => {
    try {
      await api("/api/calls/" + encodeURIComponent(again.dataset.summary) + "/summary", {});
      openCall(ref);
    } catch (error) {
      err("#summary-err", error);
    }
  });
}

async function showKnowledge() {
  threadOn();
  next.innerHTML = `${crumb("Knowledge")}
    <p class="eyebrow">Next step</p>
    <h1>Index a document</h1>
    <form id="kb-form">
      <label for="kb-name">Name</label><input id="kb-name" name="name" value="hours" required>
      <label for="kb-text">Text</label>
      <textarea id="kb-text" name="text" required>The office opens at nine.</textarea>
      <p class="meta">One document, at most 100,000 characters. Indexing is charged separately.</p>
      <button class="btn-thread" data-thread type="submit">Review this document</button>
      <div id="kb-box"></div>
      <p class="err" id="kb-err"></p>
    </form>`;
  document.querySelector("#kb-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(event.target);
    document.querySelector("[data-thread]").remove();
    document.querySelector("#kb-box").innerHTML = `<p class="meta">This indexes ${escapeHtml(data.get("name"))}. Nothing is indexed until you confirm.</p>
      <button class="btn-thread" data-thread id="index-kb" type="button">Index this document</button>`;
    document.querySelector("#index-kb").addEventListener("click", async () => {
      try {
        await api("/api/knowledge", {name: data.get("name"), text: data.get("text"), confirm: true});
        showKnowledge();
      } catch (error) {
        err("#kb-err", error);
      }
    });
  });
  try {
    await paintKnowledge();
  } catch (error) {
    err("#kb-err", error);
    view.innerHTML = `<h1>Knowledge</h1><p class="empty">No documents yet.</p>`;
  }
}
async function paintKnowledge() {
  const listed = await api("/api/knowledge");
  view.innerHTML = `<h1>Knowledge</h1>` + (listed.items.length
    ? listed.items.map((item) => `<article class="card">${pin(item.status)}<h2>${escapeHtml(item.name)}</h2>
        <p class="meta">${escapeHtml(item.sourceCount)} source</p>
        <button class="quiet" type="button" data-kb="${escapeHtml(item.ref)}">Open</button>
        <button class="quiet" type="button" data-kb-del="${escapeHtml(item.ref)}">Delete</button></article>`).join("")
    : `<p class="empty">No documents yet.</p>`);
  view.querySelectorAll("[data-kb]").forEach((button) => button.addEventListener("click", () => openKnowledge(button.dataset.kb)));
  view.querySelectorAll("[data-kb-del]").forEach((button) => button.addEventListener("click", async () => {
    const yes = window.confirm("This permanently deletes the document base. Monthly storage ends. The documents cannot be recovered.");
    if (!yes) return;
    try {
      await api("/api/knowledge/" + encodeURIComponent(button.dataset.kbDel), {confirm: true}, "DELETE");
      showKnowledge();
    } catch (error) {
      err("#kb-err", error);
    }
  }));
}
async function openKnowledge(ref) {
  let detail;
  try {
    detail = await api("/api/knowledge/" + encodeURIComponent(ref));
  } catch (error) {
    err("#kb-err", error);
    return;
  }
  view.innerHTML = `<h1>Document</h1><article class="card">${pin(detail.status)}
    <h2>${escapeHtml(detail.name)}</h2>
    <p class="meta">${escapeHtml(detail.sourceCount)} source. Status ${escapeHtml(detail.status)}.</p>
    <p class="reason">Document text is not returned.</p></article>`;
}

async function showCampaigns() {
  threadOn();
  const callers = await api("/api/numbers/callers");
  next.innerHTML = `${crumb("Campaigns")}
    <p class="eyebrow">Next step</p>
    <h1>Start a campaign</h1>
    <form id="camp-form">
      <label for="cname">Name</label><input id="cname" name="name" value="Delivery notice">
      ${callerSelect("campaign-caller", callers.items)}
      <label for="cfrom">From</label><input id="cfrom" name="from" value="+14155550100">
      <label for="cagent">Agent</label><input id="cagent" name="agent" value="reception">
      <label for="csv">CSV</label>
      <textarea id="csv" name="csv">id,to
a,+14155550102</textarea>
      <p class="meta">At most 100 people, five calls at a time. Each row is checked against the do-not-call list. A spoken message with no agent is a separate path.</p>
      <button class="btn-thread" data-thread type="submit">Review this campaign</button>
      <div id="camp-box"></div>
      <p class="err" id="camp-err"></p>
    </form>`;
  bindCallerPick("campaign-caller", "cfrom");
  document.querySelector("#camp-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(event.target);
    try {
      const checked = await api("/api/campaigns/csv", {csv: data.get("csv")});
      document.querySelector("[data-thread]").remove();
      document.querySelector("#camp-box").innerHTML = `<p class="meta">This places the agent ${escapeHtml(data.get("agent"))} on a call to ${checked.rows.length} people from ${escapeHtml(data.get("from"))}. Each person is checked against the do-not-call list. A row that never connects costs nothing. Nothing is dialed until you confirm.</p>
        <button class="btn-thread" data-thread id="start-camp" type="button">Start the campaign</button>`;
      document.querySelector("#start-camp").addEventListener("click", async () => {
        try {
          const started = await api("/api/campaigns", {
            name: data.get("name"), from: data.get("from"), assistant: data.get("agent"),
            rows: checked.rows, confirm: true,
          });
          window.__camp = started.ref;
          showCampaigns();
        } catch (error) {
          err("#camp-err", error);
        }
      });
    } catch (error) {
      err("#camp-err", error);
    }
  });
  let listed;
  try {
    listed = await api("/api/campaigns");
  } catch (error) {
    err("#camp-err", error);
    view.innerHTML = `<h1>Campaigns</h1><p class="empty">No campaigns yet.</p>`;
    return;
  }
  view.innerHTML = `<h1>Campaigns</h1>` + (listed.items.length
    ? listed.items.map((item) => {
        const lines = (item.rows || []).map((row) => {
          const detail = (row.error && row.error.message) || row.status || row.state || "";
          return `<li>${escapeHtml(row.id)} · ${escapeHtml(detail)}</li>`;
        }).join("");
        const controls = item.path === "agent" ? "" : `<button class="quiet" data-pause="${escapeHtml(item.ref)}" type="button">Pause</button>
        <button class="quiet" data-resume="${escapeHtml(item.ref)}" type="button">Resume</button>
        <button class="quiet" data-finish="${escapeHtml(item.ref)}" type="button">Finish</button>`;
        const why = item.failureReason ? `<p class="reason">${escapeHtml(item.failureReason)}</p>` : "";
        return `<article class="card">${pin(item.state)}<h2>${escapeHtml(item.name)}</h2>
        ${why}<ul>${lines}</ul>${controls}</article>`;
      }).join("")
    : `<p class="empty">No campaigns yet.</p>`);
  for (const [attr, action] of [["data-pause", "pause"], ["data-resume", "resume"], ["data-finish", "finish"]]) {
    view.querySelectorAll("[" + attr + "]").forEach((button) => button.addEventListener("click", async () => {
      try {
        await api(`/api/campaigns/${button.getAttribute(attr)}/${action}`, {});
        showCampaigns();
      } catch (error) {
        err("#camp-err", error);
      }
    }));
  }
}

async function showOptOuts() {
  threadOn();
  next.innerHTML = `${crumb("Opt-outs")}
    <p class="eyebrow">Next step</p>
    <h1>Do not call</h1>
    <form id="opt-form">
      <label for="address">Number</label>
      <input id="address" name="address" value="+14155550101">
      <button class="btn-thread" data-thread type="submit">Add to the list</button>
      <p class="err" id="opt-err"></p>
    </form>`;
  document.querySelector("#opt-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api("/api/opt-outs", {address: new FormData(event.target).get("address")});
      showOptOuts();
    } catch (error) {
      err("#opt-err", error);
    }
  });
  const listed = await api("/api/opt-outs");
  view.innerHTML = `<h1>Opt-outs</h1>` + (listed.items.length
    ? listed.items.map((item) => `<article class="card">${pin("blocked")}<h2>${escapeHtml(item.address)}</h2>
        <button class="quiet" data-drop="${escapeHtml(item.address)}" type="button">Remove</button></article>`).join("")
    : `<p class="empty">No numbers on the list.</p>`);
  view.querySelectorAll("[data-drop]").forEach((button) => button.addEventListener("click", async () => {
    const yes = window.confirm("This number can be called again.");
    if (!yes) return;
    await api("/api/opt-outs/remove", {address: button.dataset.drop, confirm: true});
    showOptOuts();
  }));
}

async function showSettings() {
  threadOn();
  const current = await api("/api/settings");
  const account = await api("/api/session");
  const signedIn = account.signedIn
    ? `<button class="quiet" id="sign-out" type="button">Sign out</button>
    <button class="quiet" id="delete-account" type="button">Delete this account</button>`
    : "";
  next.innerHTML = `${crumb("Settings")}
    <p class="eyebrow">Next step</p>
    <h1>Settings</h1>
    <form id="set-form">
      <label for="hook">Webhook endpoint</label>
      <input id="hook" name="hook" value="${escapeHtml(current.webhookEndpoint || "")}">
      ${current.needsKey ? `<p class="err">Add PRAMAAN_API_KEY to .env</p>` : ""}
      <p class="meta">${current.secretSet ? "Signing secret: Set." : "Signed callbacks are not set up yet. Set PRAMAAN_WEBHOOK_SECRET to turn them on."}</p>
      <p class="meta">${escapeHtml(current.liveChecks)}</p>
      <button class="btn-thread" data-thread type="submit">Save settings</button>
      ${signedIn}
      <p class="err" id="set-err"></p>
    </form>`;
  document.querySelector("#set-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api("/api/settings", {webhookEndpoint: new FormData(event.target).get("hook")});
      showSettings();
    } catch (error) {
      err("#set-err", error);
    }
  });
  const leave = document.querySelector("#sign-out");
  if (leave) {
    leave.addEventListener("click", async () => {
      try {
        await api("/api/sign-out", {});
        window.location.assign("/sign-in");
      } catch (error) {
        err("#set-err", error);
      }
    });
  }
  const drop = document.querySelector("#delete-account");
  if (drop) {
    drop.addEventListener("click", async () => {
      const yes = window.confirm("This permanently deletes the account.");
      if (!yes) return;
      try {
        await api("/api/account/delete", {confirm: true});
        window.location.assign("/sign-in");
      } catch (error) {
        err("#set-err", error);
      }
    });
  }
  const limits = current.limits || {};
  view.innerHTML = `<h1>Limits</h1><article class="card"><p class="meta">${escapeHtml(limits.callSeconds || "")}</p>
    <p class="meta">Campaign rows: ${escapeHtml(limits.campaignRows)}</p>
    <p class="meta">Tool requests on one call: ${escapeHtml(limits.toolRequestsPerCall)}</p></article>`;
  threadOn();
}

setTab("agents");
