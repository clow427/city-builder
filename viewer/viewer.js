let viewer;
let selected = null;
let lidarPoints = null;
let lidarVisible = false;   // start with clean CAD; "Toggle point cloud" shows raw scan

// Phase 2 relocation state
let sceneMeta = null;          // {origin:[ox,oy], crs, ...} from /api/scene_meta
let assetByName = {};          // name -> {name, type, utm:[x,y,z]} authored positions
let nameDb = {};               // object name -> viewer dbId
let placed = {};               // name -> current UTM (after moves)
let history = [];              // [{name, from}] for undo
let relocateMode = false;

// Scene selector
let scenes = [];
let currentScene = null;       // {id, label, urn, data_dir}

Autodesk.Viewing.Initializer({
  env: "AutodeskProduction2", api: "streamingV2",
  getAccessToken: cb => fetch("/api/token").then(r => r.json())
    .then(t => cb(t.access_token, t.expires_in)),
}, () => {
  viewer = new Autodesk.Viewing.GuiViewer3D(document.getElementById("v"));
  viewer.start();
  viewer.addEventListener(Autodesk.Viewing.SELECTION_CHANGED_EVENT,
    e => selected = e.dbIdArray[0] ?? null);
  installDragTool();

  fetch("/api/scenes").then(r => r.json()).then(list => {
    scenes = list || [];
    const sel = document.getElementById("scene-select");
    sel.innerHTML = scenes.map(s => `<option value="${s.id}">${s.label}</option>`).join("");
    if (scenes.length) loadScene(scenes[0]);
  });
});

function onSceneChange(id) {
  const s = scenes.find(x => x.id === id);
  if (s) loadScene(s);
}

// Tear down the current model (if any) and load a new scene's geometry +
// point cloud + asset registry. Resets all relocation/undo state since it's
// keyed to the previous scene's objects.
function loadScene(s) {
  currentScene = s;
  selected = null;
  nameDb = {}; assetByName = {}; placed = {}; history = []; sceneMeta = null;
  endRelocate();

  if (lidarPoints) {
    if (lidarVisible) viewer.impl.removeOverlay("lidar", lidarPoints);
    viewer.impl.removeOverlayScene("lidar");
    lidarPoints = null;
  }
  if (viewer.model) viewer.unloadModel(viewer.model);

  document.querySelectorAll(".loaded-badge").forEach(el => el.remove());

  Autodesk.Viewing.Document.load("urn:" + s.urn, doc => {
    viewer.addEventListener(Autodesk.Viewing.OBJECT_TREE_CREATED_EVENT, function onTree() {
      viewer.removeEventListener(Autodesk.Viewing.OBJECT_TREE_CREATED_EVENT, onTree);
      const tree = viewer.model.getInstanceTree();
      let leaves = [];
      tree.enumNodeChildren(tree.getRootId(),
        id => {
          if (tree.getChildCount(id) === 0) {
            const nm = tree.getNodeName(id);
            leaves.push(nm || id);
            if (nm) nameDb[nm] = id;     // reverse lookup for relocate/undo
          }
        },
        true);
      const b = document.createElement("div");
      b.className = "loaded-badge";
      b.style.cssText = "position:absolute;z-index:9;top:8px;right:8px;background:#DEFF00;padding:4px 8px;font:700 13px sans-serif";
      b.textContent = "objects loaded: " + leaves.length + " [" + leaves.join(", ") + "]";
      document.body.appendChild(b);
      console.log("leaf objects:", leaves);
    });
    // useConsolidation:false — consolidation merges small meshes into shared
    // GPU batches, which makes per-object fragment transforms drag neighbors.
    viewer.loadDocumentNode(doc, doc.getRoot().getDefaultGeometry(),
        { useConsolidation: false })
      .then(loadPointCloud)
      .then(setHumanView)
      .then(loadSceneData)
      .then(refreshCost);
  }, err => console.error("load failed", err));
}

// Survey data is Z-up (Z = altitude); the viewer defaults to Y-up, which makes
// the home view top-down and first-person "forward" climb vertically.
function setHumanView() {
  viewer.navigation.setWorldUpVector(new THREE.Vector3(0, 0, 1), true);
  const bb = viewer.model.getBoundingBox();
  const c = bb.center();
  const eyeZ = bb.min.z + 2;                       // ~head height above ground
  const eye = new THREE.Vector3(c.x, bb.min.y - 15, eyeZ);
  const target = new THREE.Vector3(c.x, c.y, eyeZ); // look horizontally north
  viewer.navigation.setView(eye, target);
  viewer.navigation.setCameraUpVector(new THREE.Vector3(0, 0, 1));
}

// Raw LiDAR points (XYZ+RGB) rendered inside the APS Viewer as a
// three.js (r71) PointCloud overlay — official APS blog technique.
function loadPointCloud() {
  fetch("/api/points.bin?scene=" + encodeURIComponent(currentScene.id))
    .then(r => { if (!r.ok) return null; return r.arrayBuffer(); })
    .then(buf => {
    if (!buf) { console.log("no points.bin for scene", currentScene.id); return; }
    const count = new Uint32Array(buf, 0, 1)[0];
    const positions = new Float32Array(buf, 4, count * 3);
    const rgb = new Uint8Array(buf, 4 + count * 12, count * 3);
    const colors = new Float32Array(count * 3);
    for (let i = 0; i < count * 3; i++) colors[i] = rgb[i] / 255;

    const geometry = new THREE.BufferGeometry();
    geometry.addAttribute("position", new THREE.BufferAttribute(positions, 3));
    geometry.addAttribute("color", new THREE.BufferAttribute(colors, 3));
    geometry.computeBoundingBox();
    geometry.isPoints = true;   // force gl.POINTS rendering in the Viewer

    const material = new THREE.PointCloudMaterial({
      size: 0.25, vertexColors: THREE.VertexColors,
    });
    const points = new THREE.PointCloud(geometry, material);
    // The viewer shifts the SVF2 model by a global offset (and placement
    // transform); apply the same to the overlay so points align with the model.
    const data = viewer.model.getData();
    if (data.placementWithOffset) {
      points.applyMatrix(data.placementWithOffset);
    } else if (data.globalOffset) {
      points.position.set(-data.globalOffset.x, -data.globalOffset.y, -data.globalOffset.z);
    }
    lidarPoints = points;
    viewer.impl.createOverlayScene("lidar");
    if (lidarVisible) viewer.impl.addOverlay("lidar", points);
    viewer.impl.invalidate(true, true, true);
    console.log("lidar overlay ready:", count, "points (hidden by default)");
  }).catch(err => console.error("points.bin load failed", err));
}

function togglePoints() {
  if (!lidarPoints) return;
  lidarVisible = !lidarVisible;
  if (lidarVisible) viewer.impl.addOverlay("lidar", lidarPoints);
  else viewer.impl.removeOverlay("lidar", lidarPoints);
  viewer.impl.invalidate(true, true, true);
}

function deleteCar() {
  if (selected == null) return alert("Select an object first");
  viewer.hide(selected);
}

// --- scene data + status -----------------------------------------------------

function loadSceneData() {
  const q = "?scene=" + encodeURIComponent(currentScene.id);
  return Promise.all([
    fetch("/api/scene_meta" + q).then(r => r.json()).then(m => { sceneMeta = m; }).catch(() => {}),
    fetch("/api/assets" + q).then(r => r.json())
      .then(a => (a || []).forEach(x => { assetByName[x.name] = x; })).catch(() => {}),
  ]);
}

function setStatus(msg, ms) {
  const el = document.getElementById("status");
  if (!msg) { el.style.display = "none"; return; }
  el.textContent = msg; el.style.display = "inline";
  if (ms) setTimeout(() => { el.style.display = "none"; }, ms);
}

// --- relocation: drag a movable asset across the ground ----------------------
// Movable assets = cars, trees, push-buttons — everything in the /api/assets
// registry. Ground, roads and buildings are deliberately not movable (no
// relocate op prices them). Two ways to move one: (1) press-drag-release on it,
// or (2) select it and hit "Relocate selected", then click a ground destination.

let relocatePick = null;   // {name, dbIds} armed by the button flow
let dragTool = null;

// Cosmetic sub-parts share an asset's prefix (car_01_cabin, tree_03_canopy);
// map them back to the registered anchor name.
const SUBPART_RE = /_(cabin|canopy|roof|base|body|trunk|leaves|leaf|wheels|post|pole|head|sign|panel|top|bottom)$/i;

function assetBaseFor(name) {
  if (!name) return null;
  if (assetByName[name]) return name;
  const s = name.replace(SUBPART_RE, "");
  return (s !== name && assetByName[s]) ? s : null;
}

// Current viewer-world XY of an asset's anchor: authored UTM minus scene origin
// minus the viewer's global offset, plus any move already applied.
function assetWorldXY(name) {
  const reg = assetByName[name];
  if (!reg || !sceneMeta) return null;
  const o = sceneMeta.origin || [0, 0];
  const off = (viewer.model.getData().globalOffset) || { x: 0, y: 0 };
  const cur = placed[name] || reg.utm;
  return { x: cur[0] - o[0] - off.x, y: cur[1] - o[1] - off.y };
}

function nearestAsset(pt, maxDist) {
  let best = null, bestD = maxDist;
  for (const name in assetByName) {
    const w = assetWorldXY(name);
    if (!w) continue;
    const d = Math.hypot(w.x - pt.x, w.y - pt.y);
    if (d <= bestD) { bestD = d; best = name; }
  }
  return best;
}

// All dbIds that form an asset: the anchor plus same-prefix sub-parts, so the
// whole car (body + cabin) moves as one rigid piece.
function dbIdsForAsset(base) {
  const ids = [];
  for (const nm in nameDb) {
    if (nm === base || nm.startsWith(base + "_")) ids.push(nameDb[nm]);
  }
  return ids;
}

// Resolve a clicked/hovered dbId to {name, dbIds}, or null if it's not movable.
// Tries the node name and its ancestors first, then falls back to proximity
// against authored asset positions — so drag still works on SVF2 trees whose
// node names don't line up with the registry.
function pickAsset(dbId, worldPt) {
  const tree = viewer.model && viewer.model.getInstanceTree();
  let base = null;
  if (tree && dbId != null) {
    let id = dbId, guard = 0;
    while (id != null && guard++ < 32) {
      const b = assetBaseFor(tree.getNodeName(id));
      if (b) { base = b; break; }
      const p = tree.getNodeParentId(id);
      if (p === id) break;
      id = p;
    }
  }
  if (!base && worldPt) base = nearestAsset(worldPt, 1.75);
  if (!base) return null;
  let dbIds = dbIdsForAsset(base);
  if (!dbIds.length && dbId != null) dbIds = [dbId];   // name-mangled fallback
  return dbIds.length ? { name: base, dbIds } : null;
}

// Move an asset to an absolute offset (metres) from its authored anchor. Deltas
// are translation-invariant, so the same (dx,dy) is applied to every fragment
// of every sub-part — the whole asset slides rigidly.
function moveAsset(dbIds, dx, dy) {
  const tree = viewer.model.getInstanceTree();
  for (const dbId of dbIds) {
    tree.enumNodeFragments(dbId, fragId => {
      const fp = viewer.impl.getFragmentProxy(viewer.model, fragId);
      fp.getAnimTransform();
      fp.position.x = dx; fp.position.y = dy;
      fp.updateAnimTransform();
    });
  }
  viewer.impl.invalidate(true, true, true);
}

function currentOffset(name, reg) {
  const cur = placed[name];
  return cur ? { x: cur[0] - reg.utm[0], y: cur[1] - reg.utm[1] } : { x: 0, y: 0 };
}

// Persist a finished move (as a UTM delta) and re-price; skips no-op nudges.
function commitMove(name, dbIds, fromUtm, off) {
  const reg = assetByName[name];
  const toUtm = [reg.utm[0] + off.x, reg.utm[1] + off.y, reg.utm[2]];
  if (Math.hypot(toUtm[0] - fromUtm[0], toUtm[1] - fromUtm[1]) < 0.05) {
    setStatus("", 0);
    return;
  }
  history.push({ name, dbIds, from: fromUtm });
  placed[name] = toUtm;
  postEdit({ op: "relocate", target: name, asset_type: reg.type,
             from_utm: fromUtm, to_utm: toUtm });
}

// --- direct drag tool: press on an asset, drag, release to place -------------

function installDragTool() {
  if (dragTool || !viewer.toolController) return;
  let drag = null;
  const ground = ev => viewer.impl.intersectGround(ev.clientX, ev.clientY);
  const hitPick = ev => {
    const hit = viewer.impl.hitTest(ev.canvasX, ev.canvasY, false);
    return hit ? pickAsset(hit.dbId, hit.intersectPoint || hit.point) : null;
  };

  dragTool = {
    getNames: () => ["sf-drag"],
    getName: () => "sf-drag",
    getPriority: () => 100,            // above the orbit/navigation tools
    activate: () => {},
    deactivate: () => { drag = null; },

    handleButtonDown: (ev, button) => {
      if (button !== 0 || ev.shiftKey || ev.ctrlKey || ev.altKey || ev.metaKey) return false;
      const pick = hitPick(ev);
      const g = pick && ground(ev);
      if (!pick || !g) return false;   // not an asset -> let the camera orbit
      const reg = assetByName[pick.name];
      drag = {
        name: pick.name, dbIds: pick.dbIds,
        fromUtm: placed[pick.name] || reg.utm,
        startGround: { x: g.x, y: g.y },
        startOff: currentOffset(pick.name, reg),
      };
      drag.lastOff = drag.startOff;
      if (nameDb[pick.name] != null) viewer.select(nameDb[pick.name]);
      viewer.container.style.cursor = "grabbing";
      setStatus(`Dragging "${pick.name}" — release to place`);
      return true;                     // consume so the camera doesn't move
    },

    handleMouseMove: ev => {
      if (!drag) {                      // hover: hint that the asset is grabbable
        if (viewer.container.style.cursor !== "grabbing")
          viewer.container.style.cursor = hitPick(ev) ? "grab" : "";
        return false;
      }
      const g = ground(ev);
      if (g) {                          // keep the grab point under the cursor
        const dx = drag.startOff.x + (g.x - drag.startGround.x);
        const dy = drag.startOff.y + (g.y - drag.startGround.y);
        moveAsset(drag.dbIds, dx, dy);
        drag.lastOff = { x: dx, y: dy };
      }
      return true;
    },

    handleButtonUp: (ev, button) => {
      if (!drag) return false;
      const d = drag; drag = null;
      viewer.container.style.cursor = "grab";
      commitMove(d.name, d.dbIds, d.fromUtm, d.lastOff);
      return true;
    },
  };

  try {
    viewer.toolController.registerTool(dragTool);
    viewer.toolController.activateTool("sf-drag");
  } catch (e) { console.warn("drag tool install failed", e); }
}

// --- button flow: select an asset, then click a ground destination -----------

function startRelocate() {
  if (selected == null) return alert("Select an asset first — or just drag it.");
  const pick = pickAsset(selected, null);
  if (!pick) return alert("That object isn't a movable asset. Movable: cars, trees, push-buttons. Drag one directly, or select it first.");
  relocatePick = pick;
  relocateMode = true;
  setStatus(`Click the ground to place "${pick.name}"  (Esc to cancel)`);
  viewer.container.addEventListener("click", onPlace, true);
  document.addEventListener("keydown", onCancelRelocate, true);
}

function onCancelRelocate(ev) {
  if (ev.key !== "Escape") return;
  endRelocate();
  setStatus("relocate cancelled", 1500);
}

function endRelocate() {
  relocateMode = false;
  relocatePick = null;
  viewer.container.removeEventListener("click", onPlace, true);
  document.removeEventListener("keydown", onCancelRelocate, true);
}

// Convert a clicked ground point (viewer world coords) to UTM, move the armed
// asset there, persist the relocate edit, and re-price.
function onPlace(ev) {
  ev.stopPropagation(); ev.preventDefault();
  const pick = relocatePick;
  endRelocate();
  const g = viewer.impl.intersectGround(ev.clientX, ev.clientY);
  if (!g || !pick || !sceneMeta) { setStatus("placement failed", 2500); return; }

  // model space = world + globalOffset (the viewer subtracts it for rendering);
  // UTM = model-local + scene origin. Deltas are translation-invariant.
  const reg = assetByName[pick.name];
  const off = (viewer.model.getData().globalOffset) || { x: 0, y: 0 };
  const o = sceneMeta.origin || [0, 0];
  const fromUtm = placed[pick.name] || reg.utm;
  const delta = { x: g.x + off.x + o[0] - reg.utm[0], y: g.y + off.y + o[1] - reg.utm[1] };
  moveAsset(pick.dbIds, delta.x, delta.y);
  commitMove(pick.name, pick.dbIds, fromUtm, delta);
}

function postEdit(edit) {
  fetch("/api/scenario?scene=" + encodeURIComponent(currentScene.id), {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ edit }),
  }).then(r => r.json()).then(res => {
    renderCost(res.estimate);
    const w = res.warnings || [];
    setStatus(w.length ? "⚠ " + w.join("; ") : `moved ${edit.target}`, w.length ? 6000 : 2500);
  }).catch(err => { setStatus("save failed", 3000); console.error(err); });
}

function undoEdit() {
  fetch("/api/scenario?scene=" + encodeURIComponent(currentScene.id), {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ undo: true }),
  }).then(r => r.json()).then(res => {
    renderCost(res.estimate);
    const h = history.pop();
    if (h && assetByName[h.name]) {
      const reg = assetByName[h.name];
      placed[h.name] = h.from;
      const dbIds = (h.dbIds && h.dbIds.length) ? h.dbIds : dbIdsForAsset(h.name);
      moveAsset(dbIds, h.from[0] - reg.utm[0], h.from[1] - reg.utm[1]);
    }
    setStatus("undid last edit", 2000);
  }).catch(err => { setStatus("undo failed", 3000); console.error(err); });
}

// Debug/verification hook — exposes what the loaded SVF2 tree offers vs the
// registry, and whether each asset resolves for dragging.
window.__sfDebug = function () {
  const out = { assets: Object.keys(assetByName), leaves: Object.keys(nameDb), resolved: {} };
  for (const name in assetByName) {
    const id = nameDb[name];
    const p = id != null ? pickAsset(id, null) : null;
    out.resolved[name] = { dbId: id ?? null, resolvesTo: p ? p.name : null, parts: p ? p.dbIds.length : 0 };
  }
  return out;
};

// --- live cost panel (running total of the proposal) -------------------------

function toggleCost() {
  const panel = document.getElementById("cost");
  const show = panel.style.display === "none";
  panel.style.display = show ? "block" : "none";
  if (show) refreshCost();
}

function refreshCost() {
  return fetch("/api/scenario?scene=" + encodeURIComponent(currentScene.id)).then(r => r.json())
    .then(res => renderCost(res.estimate)).catch(() => {});
}

function renderCost(rep) {
  const body = document.getElementById("cost-body");
  if (!rep || rep.error) { body.textContent = (rep && rep.error) || "no estimate"; return; }
  if (!rep.line_items || !rep.line_items.length) { body.textContent = "No edits yet — relocate an asset to start a proposal."; return; }
  const usd = n => "$" + Number(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  let html = "<table style='border-collapse:collapse;width:100%'>";
  for (const li of rep.line_items) {
    html += `<tr><td style='padding:1px 6px 1px 0'>${li.description}</td>`
          + `<td style='text-align:right;padding:1px 0'>${usd(li.amount)}</td></tr>`;
  }
  html += `<tr><td style='padding-top:6px;font-weight:700;color:#DEFF00'>Total</td>`
        + `<td style='padding-top:6px;text-align:right;font-weight:700;color:#DEFF00'>${usd(rep.total)}</td></tr>`;
  html += "</table>";
  body.innerHTML = html;
}
