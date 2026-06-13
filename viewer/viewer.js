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
  assetFrags = null;
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
    // Build per-asset fragment clusters once all geometry has streamed in (so
    // fragment world-bounds are final) and the asset registry is loaded. If the
    // registry isn't ready yet, the first grab builds them lazily instead.
    viewer.addEventListener(Autodesk.Viewing.GEOMETRY_LOADED_EVENT, function onGeom() {
      viewer.removeEventListener(Autodesk.Viewing.GEOMETRY_LOADED_EVENT, onGeom);
      if (Object.keys(assetByName).length) buildAssetFrags();
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

// --- relocation: drag a single movable asset across the ground --------------
// Movable assets = cars, trees, push-buttons — everything in /api/assets.
// Ground, roads and buildings are deliberately not movable (no relocate op
// prices them).
//
// The deployed SVF2 model groups geometry by MATERIAL, not by object: all cars
// live under one "car" node, every tree canopy under "canopy", etc. So there is
// no per-car dbId to grab — moving a dbId would move every car at once. Instead
// we work at the FRAGMENT level: each car/tree is a co-located cluster of
// fragments, so we move only the fragments within a small radius of the grabbed
// asset's authored position (radius < the ~6 m spacing between assets). Clusters
// are computed once at load, from authored positions, so they stay stable as
// assets are moved around. This also works on object-separated models.

let relocatePick = null;   // {name, fragIds, reg} armed by the button flow
let dragTool = null;
let assetFrags = null;     // name -> [fragId], built once per scene (see below)

const CLUSTER_R = 3.0;     // metres; half the inter-asset spacing
// Names of non-movable geometry — matches both material-grouped ("asphalt",
// "wall") and object-separated ("ground_road", "building_01") models.
const NONMOVABLE_RE = /(ground|road|sidewalk|building|wall|roof|asphalt|concrete|grass|pavement|curb|terrain)/i;

// Viewer-world XY of an asset, using a given UTM (authored or current).
function utmToWorldXY(utm) {
  const o = (sceneMeta && sceneMeta.origin) || [0, 0];
  const off = (viewer.model.getData().globalOffset) || { x: 0, y: 0 };
  return { x: utm[0] - o[0] - off.x, y: utm[1] - o[1] - off.y };
}

function nearestAsset(pt, maxDist) {
  let best = null, bestD = maxDist;
  for (const name in assetByName) {
    const reg = assetByName[name];
    const w = utmToWorldXY(placed[name] || reg.utm);
    const d = Math.hypot(w.x - pt.x, w.y - pt.y);
    if (d <= bestD) { bestD = d; best = name; }
  }
  return best;
}

function movableDbIds() {
  const ids = [];
  for (const nm in nameDb) if (!NONMOVABLE_RE.test(nm)) ids.push(nameDb[nm]);
  return ids;
}

// Map each registered asset to the set of fragments sitting on top of it, by
// proximity to its AUTHORED position. Built once, before anything has moved, so
// fragment world-bounds reflect their authored placement.
function buildAssetFrags() {
  try {
    const tree = viewer.model.getInstanceTree();
    const fl = viewer.model.getFragmentList();
    if (!tree || !fl) return;
    const box = new THREE.Box3();
    const frags = [];               // {fragId, x, y} for all movable fragments
    for (const dbId of movableDbIds()) {
      tree.enumNodeFragments(dbId, fragId => {
        fl.getWorldBounds(fragId, box);
        const c = box.center();
        frags.push({ fragId, x: c.x, y: c.y });
      });
    }
    assetFrags = {};
    for (const name in assetByName) {
      const w = utmToWorldXY(assetByName[name].utm);
      assetFrags[name] = frags
        .filter(f => Math.hypot(f.x - w.x, f.y - w.y) <= CLUSTER_R)
        .map(f => f.fragId);
    }
    console.log("asset fragment clusters:",
      Object.fromEntries(Object.entries(assetFrags).map(([k, v]) => [k, v.length])));
  } catch (e) { console.warn("buildAssetFrags failed", e); assetFrags = {}; }
}

function fragsForAsset(name) {
  if (!assetFrags) buildAssetFrags();
  return (assetFrags && assetFrags[name]) || [];
}

// The single movable asset under a hit (from hitTest), or null. Cheap: a node
// name check plus nearest-asset lookup — does NOT build fragment clusters, so
// it's safe to call on every hover.
function assetNameAt(hit) {
  if (!hit || !sceneMeta) return null;
  const pt = hit.intersectPoint || hit.point;
  if (!pt) return null;
  const tree = viewer.model.getInstanceTree();
  const nm = (tree && tree.getNodeName(hit.dbId)) || "";
  if (NONMOVABLE_RE.test(nm)) return null;          // grabbed ground/building
  return nearestAsset(pt, CLUSTER_R + 1.0);         // must be near a real asset
}

// Full pick for a grab/placement: {name, fragIds, reg}. Builds the asset's
// fragment cluster (lazily, once) on top of assetNameAt.
function pickAt(hit) {
  const name = assetNameAt(hit);
  if (!name) return null;
  const fragIds = fragsForAsset(name).slice();
  if (hit.fragId != null && fragIds.indexOf(hit.fragId) < 0) fragIds.push(hit.fragId);
  return fragIds.length ? { name, fragIds, reg: assetByName[name] } : null;
}

// Translate a set of fragments by an absolute offset (metres) from their
// authored placement. The same (dx,dy) applied to each fragment slides the
// asset rigidly.
function moveFrags(fragIds, dx, dy) {
  for (const fragId of fragIds) {
    const fp = viewer.impl.getFragmentProxy(viewer.model, fragId);
    fp.getAnimTransform();
    fp.position.x = dx; fp.position.y = dy;
    fp.updateAnimTransform();
  }
  viewer.impl.invalidate(true, true, true);
}

function currentOffset(name, reg) {
  const cur = placed[name];
  return cur ? { x: cur[0] - reg.utm[0], y: cur[1] - reg.utm[1] } : { x: 0, y: 0 };
}

// Persist a finished move (as a UTM delta) and re-price; skips no-op nudges.
function commitMove(name, fragIds, fromUtm, off) {
  const reg = assetByName[name];
  const toUtm = [reg.utm[0] + off.x, reg.utm[1] + off.y, reg.utm[2]];
  if (Math.hypot(toUtm[0] - fromUtm[0], toUtm[1] - fromUtm[1]) < 0.05) {
    setStatus("", 0);
    return;
  }
  history.push({ name, fragIds, from: fromUtm });
  placed[name] = toUtm;
  postEdit({ op: "relocate", target: name, asset_type: reg.type,
             from_utm: fromUtm, to_utm: toUtm });
}

// --- direct drag tool: press on an asset, drag, release to place -------------

function installDragTool() {
  if (dragTool || !viewer.toolController) return;
  let drag = null;
  const ground = ev => viewer.impl.intersectGround(ev.clientX, ev.clientY);
  const pickEv = ev => pickAt(viewer.impl.hitTest(ev.canvasX, ev.canvasY, false));

  dragTool = {
    getNames: () => ["sf-drag"],
    getName: () => "sf-drag",
    getPriority: () => 100,            // above the orbit/navigation tools
    activate: () => {},
    deactivate: () => { drag = null; },

    handleButtonDown: (ev, button) => {
      if (relocateMode) return false;  // button flow owns clicks while armed
      if (button !== 0 || ev.shiftKey || ev.ctrlKey || ev.altKey || ev.metaKey) return false;
      const pick = pickEv(ev);
      const g = pick && ground(ev);
      if (!pick || !g) return false;   // not an asset -> let the camera orbit
      drag = {
        name: pick.name, fragIds: pick.fragIds,
        fromUtm: placed[pick.name] || pick.reg.utm,
        startGround: { x: g.x, y: g.y },
        startOff: currentOffset(pick.name, pick.reg),
      };
      drag.lastOff = drag.startOff;
      viewer.container.style.cursor = "grabbing";
      setStatus(`Dragging "${pick.name}" — release to place`);
      return true;                     // consume so the camera doesn't move
    },

    handleMouseMove: ev => {
      if (!drag) {                      // hover: hint that the asset is grabbable
        if (!relocateMode && viewer.container.style.cursor !== "grabbing") {
          const hit = viewer.impl.hitTest(ev.canvasX, ev.canvasY, false);
          viewer.container.style.cursor = assetNameAt(hit) ? "grab" : "";
        }
        return false;
      }
      const g = ground(ev);
      if (g) {                          // keep the grab point under the cursor
        const dx = drag.startOff.x + (g.x - drag.startGround.x);
        const dy = drag.startOff.y + (g.y - drag.startGround.y);
        moveFrags(drag.fragIds, dx, dy);
        drag.lastOff = { x: dx, y: dy };
      }
      return true;
    },

    handleButtonUp: (ev, button) => {
      if (!drag) return false;
      const d = drag; drag = null;
      viewer.container.style.cursor = "grab";
      commitMove(d.name, d.fragIds, d.fromUtm, d.lastOff);
      return true;
    },
  };

  try {
    viewer.toolController.registerTool(dragTool);
    viewer.toolController.activateTool("sf-drag");
  } catch (e) { console.warn("drag tool install failed", e); }
}

// --- button flow: click an asset to pick it up, then click its destination ---
// (Same single-asset picking as drag; handy on touch / when orbiting is fiddly.)

function startRelocate() {
  relocatePick = null;
  relocateMode = true;
  setStatus("Relocate: click an asset to pick it up  (Esc to cancel)");
  viewer.container.addEventListener("click", onRelocateClick, true);
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
  viewer.container.removeEventListener("click", onRelocateClick, true);
  document.removeEventListener("keydown", onCancelRelocate, true);
}

// First click picks the asset under the cursor; second click places it on the
// ground. (Container is full-window, so client coords == canvas coords.)
function onRelocateClick(ev) {
  ev.stopPropagation(); ev.preventDefault();
  if (!relocatePick) {
    const pick = pickAt(viewer.impl.hitTest(ev.clientX, ev.clientY, false));
    if (!pick) { setStatus("Not a movable asset — click a car, tree or push-button", 2500); return; }
    relocatePick = { ...pick, fromUtm: placed[pick.name] || pick.reg.utm };
    setStatus(`Picked "${pick.name}" — click its destination  (Esc to cancel)`);
    return;
  }
  const g = viewer.impl.intersectGround(ev.clientX, ev.clientY);
  if (!g || !sceneMeta) { setStatus("placement failed — click on the ground", 2500); return; }
  const pick = relocatePick;
  endRelocate();
  // ground point -> UTM -> offset from the asset's authored anchor
  const off = (viewer.model.getData().globalOffset) || { x: 0, y: 0 };
  const o = sceneMeta.origin || [0, 0];
  const delta = { x: g.x + off.x + o[0] - pick.reg.utm[0],
                  y: g.y + off.y + o[1] - pick.reg.utm[1] };
  moveFrags(pick.fragIds, delta.x, delta.y);
  commitMove(pick.name, pick.fragIds, pick.fromUtm, delta);
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
      const fragIds = (h.fragIds && h.fragIds.length) ? h.fragIds : fragsForAsset(h.name);
      moveFrags(fragIds, h.from[0] - reg.utm[0], h.from[1] - reg.utm[1]);
    }
    setStatus("undid last edit", 2000);
  }).catch(err => { setStatus("undo failed", 3000); console.error(err); });
}

// Debug/verification hook — the SVF2 node names, which are movable, and how
// many fragments cluster onto each asset (should be a small handful, not all).
window.__sfDebug = function () {
  if (!assetFrags) buildAssetFrags();
  const clusters = {};
  for (const name in assetByName) clusters[name] = (assetFrags[name] || []).length;
  return {
    assets: Object.keys(assetByName),
    nodes: Object.keys(nameDb),
    movableNodeIds: movableDbIds(),
    fragmentsPerAsset: clusters,
  };
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
