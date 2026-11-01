/* Cockpit module: Incus & LXC instances with a web console. */
/* global cockpit, Terminal, FitAddon */
"use strict";

const $ = id => document.getElementById(id);
const SPAWN_OPTS = { superuser: "try", err: "message" };
const NAME_RE = /^[A-Za-z][A-Za-z0-9-]{0,62}$/;
const field = (form, n) => form.elements.namedItem(n);
const SHELL_CMD = "if command -v bash >/dev/null 2>&1; then exec bash -l; else exec sh -l; fi";

function run(argv) {
    return cockpit.spawn(argv, SPAWN_OPTS);
}

function errText(ex) {
    if (!ex) return "erreur inconnue";
    if (ex.problem === "not-found") return "commande introuvable sur le serveur";
    if (ex.problem === "access-denied") return "accès refusé (activez l'accès administrateur)";
    return (ex.message || ex.problem || String(ex)).trim();
}

function el(tag, props, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
        if (k === "class") node.className = v;
        else if (k === "onclick") node.addEventListener("click", v);
        else node.setAttribute(k, v);
    }
    for (const c of children) {
        if (c === null || c === undefined) continue;
        node.append(c instanceof Node ? c : String(c));
    }
    return node;
}

function setMsg(id, text, kind) {
    const m = $(id);
    m.textContent = text || "";
    m.className = "msg" + (kind ? " " + kind : "");
}

/* ---------- Incus ---------- */

function incusIPv4(inst) {
    const nets = (inst.state && inst.state.network) || {};
    const out = [];
    for (const [ifname, net] of Object.entries(nets)) {
        if (ifname === "lo") continue;
        for (const a of net.addresses || []) {
            if (a.family === "inet" && a.scope === "global") out.push(a.address);
        }
    }
    return out.join(", ");
}

async function listIncus() {
    const raw = await run(["incus", "list", "--format", "json"]);
    return JSON.parse(raw).map(i => ({
        name: i.name,
        status: i.status || "",
        type: i.type === "virtual-machine" ? "VM" : "conteneur",
        ipv4: incusIPv4(i),
        image: (i.config && (i.config["image.description"] || i.config["image.os"])) || "",
    }));
}

function incusActions(inst) {
    const running = inst.status.toLowerCase() === "running";
    const n = inst.name;
    const a = el("div", { class: "actions" });
    const act = (label, argv, extra) => a.append(el("button", {
        type: "button", class: "btn" + (extra ? " " + extra : ""),
        onclick: () => doAction("incus-msg", `${label} ${n}`, argv),
    }, label));

    if (running) {
        a.append(el("button", {
            type: "button", class: "btn primary",
            onclick: () => openTerminal(`Shell — incus/${n}`, ["incus", "exec", n, "--", "sh", "-c", SHELL_CMD], ""),
        }, "Shell"));
        a.append(el("button", {
            type: "button", class: "btn",
            onclick: () => openTerminal(`Console — incus/${n}`, ["incus", "console", n], "Quitter la console : Ctrl+a puis q"),
        }, "Console"));
        act("Redémarrer", ["incus", "restart", n]);
        act("Arrêter", ["incus", "stop", n]);
    } else {
        act("Démarrer", ["incus", "start", n]);
    }
    a.append(el("button", {
        type: "button", class: "btn danger",
        onclick: () => {
            if (window.confirm(`Supprimer définitivement l'instance Incus « ${n} » ?`))
                doAction("incus-msg", `Suppression de ${n}`, ["incus", "delete", "--force", n]);
        },
    }, "Supprimer"));
    return a;
}

function renderIncus(list) {
    const tb = $("incus-list");
    tb.replaceChildren();
    if (!list.length) {
        tb.append(el("tr", {}, el("td", { class: "empty", colspan: "6" }, "Aucune instance Incus.")));
        return;
    }
    for (const i of list) {
        tb.append(el("tr", {},
            el("td", {}, i.name),
            el("td", {}, el("span", { class: "state " + i.status.toLowerCase() }, i.status)),
            el("td", {}, i.type),
            el("td", {}, i.ipv4 || "—"),
            el("td", {}, i.image || "—"),
            el("td", { class: "actions-col" }, incusActions(i))));
    }
}

/* ---------- LXC ---------- */

async function listLxc() {
    const raw = await run(["lxc-ls", "--fancy", "--fancy-format", "NAME,STATE,AUTOSTART,IPV4"]);
    const lines = raw.split("\n").filter(l => l.trim());
    if (lines.length && /^NAME\s/.test(lines[0])) lines.shift();
    return lines.map(l => {
        const p = l.trim().split(/\s+/);
        const ipv4 = p.slice(3).join(" ");
        return { name: p[0], status: p[1] || "", autostart: p[2] || "", ipv4: ipv4 === "-" ? "" : ipv4 };
    });
}

function lxcActions(ct) {
    const running = ct.status.toUpperCase() === "RUNNING";
    const n = ct.name;
    const a = el("div", { class: "actions" });
    const act = (label, argv, extra) => a.append(el("button", {
        type: "button", class: "btn" + (extra ? " " + extra : ""),
        onclick: () => doAction("lxc-msg", `${label} ${n}`, argv),
    }, label));

    if (running) {
        a.append(el("button", {
            type: "button", class: "btn primary",
            onclick: () => openTerminal(`Shell — lxc/${n}`, ["lxc-attach", "-n", n, "--", "sh", "-c", SHELL_CMD], ""),
        }, "Shell"));
        a.append(el("button", {
            type: "button", class: "btn",
            onclick: () => openTerminal(`Console — lxc/${n}`, ["lxc-console", "-n", n, "-t", "0"], "Quitter la console : Ctrl+a puis q"),
        }, "Console"));
        act("Arrêter", ["lxc-stop", "-n", n]);
    } else {
        act("Démarrer", ["lxc-start", "-n", n]);
    }
    a.append(el("button", {
        type: "button", class: "btn danger",
        onclick: () => {
            if (window.confirm(`Supprimer définitivement le conteneur LXC « ${n} » ?`))
                doAction("lxc-msg", `Suppression de ${n}`, ["lxc-destroy", "-f", "-n", n]);
        },
    }, "Supprimer"));
    return a;
}

function renderLxc(list) {
    const tb = $("lxc-list");
    tb.replaceChildren();
    if (!list.length) {
        tb.append(el("tr", {}, el("td", { class: "empty", colspan: "5" }, "Aucun conteneur LXC.")));
        return;
    }
    for (const c of list) {
        tb.append(el("tr", {},
            el("td", {}, c.name),
            el("td", {}, el("span", { class: "state " + c.status.toLowerCase() }, c.status)),
            el("td", {}, c.ipv4 || "—"),
            el("td", {}, c.autostart === "1" ? "oui" : "non"),
            el("td", { class: "actions-col" }, lxcActions(c))));
    }
}

/* ---------- refresh / actions ---------- */

let busy = 0;
let lastError = { incus: "", lxc: "" };

async function refresh() {
    const [inc, lxc] = await Promise.allSettled([listIncus(), listLxc()]);
    if (inc.status === "fulfilled") {
        renderIncus(inc.value);
        if (lastError.incus) { setMsg("incus-msg", ""); lastError.incus = ""; }
    } else if (!busy) {
        lastError.incus = errText(inc.reason);
        setMsg("incus-msg", "Incus : " + lastError.incus, "error");
    }
    if (lxc.status === "fulfilled") {
        renderLxc(lxc.value);
        if (lastError.lxc) { setMsg("lxc-msg", ""); lastError.lxc = ""; }
    } else if (!busy) {
        lastError.lxc = errText(lxc.reason);
        setMsg("lxc-msg", "LXC : " + lastError.lxc, "error");
    }
}

async function doAction(msgId, label, argv) {
    busy++;
    setMsg(msgId, label + "…", "busy");
    try {
        await run(argv);
        setMsg(msgId, label + " : terminé.");
    } catch (ex) {
        setMsg(msgId, label + " : " + errText(ex), "error");
    } finally {
        busy--;
        refresh();
    }
}

function setupCreateForms() {
    $("incus-create").addEventListener("submit", ev => {
        ev.preventDefault();
        const f = ev.target;
        const name = field(f, "name").value.trim();
        const image = field(f, "image").value.trim();
        if (!NAME_RE.test(name)) return setMsg("incus-msg", "Nom invalide (lettres, chiffres, tirets).", "error");
        const argv = ["incus", "launch", image, name];
        if (field(f, "vm").checked) argv.push("--vm");
        const btn = f.querySelector("button[type=submit]");
        btn.disabled = true;
        doAction("incus-msg", `Création de ${name} (${image})`, argv).finally(() => {
            btn.disabled = false;
            field(f, "name").value = "";
        });
    });

    $("lxc-create").addEventListener("submit", ev => {
        ev.preventDefault();
        const f = ev.target;
        const name = field(f, "name").value.trim();
        if (!NAME_RE.test(name)) return setMsg("lxc-msg", "Nom invalide (lettres, chiffres, tirets).", "error");
        const argv = ["lxc-create", "-n", name, "-t", "download", "--",
            "-d", field(f, "dist").value.trim(), "-r", field(f, "release").value.trim(), "-a", field(f, "arch").value.trim()];
        const btn = f.querySelector("button[type=submit]");
        btn.disabled = true;
        doAction("lxc-msg", `Création de ${name}`, argv).finally(() => {
            btn.disabled = false;
            field(f, "name").value = "";
        });
    });
}

/* ---------- terminal ---------- */

let session = null;

function closeTerminal() {
    if (!session) return;
    const s = session;
    session = null;
    s.ro.disconnect();
    s.subs.forEach(d => d.dispose());
    try { s.channel.close(); } catch (_) { /* already closed */ }
    s.term.dispose();
    $("term-panel").hidden = true;
}

function openTerminal(title, argv, hint) {
    closeTerminal();
    $("term-title").textContent = title;
    $("term-hint").textContent = hint || "";
    $("term-panel").hidden = false;

    const box = $("term");
    box.replaceChildren();
    const term = new Terminal({
        cursorBlink: true,
        fontSize: 14,
        fontFamily: "Menlo, 'DejaVu Sans Mono', 'Liberation Mono', monospace",
        scrollback: 5000,
        theme: { background: "#000000" },
    });
    const fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(box);
    try { fit.fit(); } catch (_) { /* not yet laid out */ }

    const channel = cockpit.channel({
        payload: "stream",
        spawn: argv,
        pty: true,
        superuser: "try",
        environ: ["TERM=xterm-256color"],
        window: { rows: term.rows, cols: term.cols },
    });
    channel.addEventListener("message", (_ev, data) => term.write(data));
    channel.addEventListener("close", (_ev, options) => {
        const why = options && options.problem ? " : " + errText(options) : "";
        term.write(`\r\n\x1b[33m[session terminée${why}]\x1b[0m\r\n`);
    });

    const subs = [
        term.onData(data => channel.send(data)),
        term.onResize(({ rows, cols }) => channel.control({ window: { rows, cols } })),
    ];
    const ro = new ResizeObserver(() => { try { fit.fit(); } catch (_) { /* hidden */ } });
    ro.observe(box);

    session = { term, channel, subs, ro };
    $("term-panel").scrollIntoView({ behavior: "smooth", block: "start" });
    term.focus();
}

/* ---------- init ---------- */

function showVersions() {
    run(["incus", "version"]).then(out => {
        const m = out.match(/Server version:\s*(\S+)/);
        $("incus-version").textContent = m ? "serveur " + m[1] : "";
    }).catch(() => {});
    run(["lxc-ls", "--version"]).then(out => {
        $("lxc-version").textContent = "version " + out.trim();
    }).catch(() => {});
    run(["uname", "-m"]).then(out => {
        const map = { x86_64: "amd64", aarch64: "arm64", armv7l: "armhf", ppc64le: "ppc64el", s390x: "s390x" };
        const arch = map[out.trim()];
        if (arch) field($("lxc-create"), "arch").value = arch;
    }).catch(() => {});
}

function init() {
    const perm = cockpit.permission({ admin: true });
    const updatePerm = () => { $("admin-hint").hidden = perm.allowed !== false; };
    perm.addEventListener("changed", () => { updatePerm(); refresh(); });
    updatePerm();

    $("refresh").addEventListener("click", refresh);
    $("term-close").addEventListener("click", closeTerminal);
    setupCreateForms();
    showVersions();
    refresh();
    window.setInterval(() => { if (!document.hidden) refresh(); }, 5000);
}

document.addEventListener("DOMContentLoaded", init);
