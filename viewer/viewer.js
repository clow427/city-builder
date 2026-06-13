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
let removed = {};              // name -> fragIds[] for fragment-level hidden assets
let relocateMode = false;
let removeMode = false;

// Scene selector
let scenes = [];
let currentScene = null;       // {id, label, urn, data_dir}

// Road-building state (Cities:Skylines-style "extend road" tool)
let roadBuildMode = false;     // armed by the "Build road" button
let roadStart = null;          // world point of the first click {x,y,z}
let roads = [];                // [{name, mesh}] live road overlays
let roadPreview = null;        // rubber-band mesh shown while aiming
let roadOverlayReady = false;  // is the "roads" overlay scene created
let roadSeq = 0;               // highest road_NN index seen, for naming
const ROAD_LIFT = 0.04;        // metres above the sampled surface (anti z-fight)
const ROAD_COLOR = 0x222222;   // dark asphalt, matches existing streets

// Green terrain fill tool — two-corner rectangle, draped over surface
let greenBuildMode = false;
let greenStart = null;         // world point of the first corner {x,y,z}
let greens = [];               // [{name, mesh}] live green overlays
let greenPreview = null;
let greenOverlayReady = false;
let greenSeq = 0;
const GREEN_LIFT = 0.10;       // higher than ROAD_LIFT so green covers roads
const GREEN_COLOR = 0x3a7d3a;

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
  nameDb = {}; assetByName = {}; placed = {}; history = []; removed = {}; sceneMeta = null;
  assetFrags = null;
  endRelocate();
  endRemoveMode();
  endBuildRoad();
  endBuildGreen();
  clearRoads();
  clearGreens();

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
      .then(loadRoads)
      .then(loadGreens)
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

// Scale fragments to 0 / 1 to hide or restore a specific asset without touching
// other assets that share the same material node (same as how drag works).
function hideFrags(fragIds) {
  for (const fragId of fragIds) {
    const fp = viewer.impl.getFragmentProxy(viewer.model, fragId);
    fp.getAnimTransform();
    fp.scale.x = 0; fp.scale.y = 0; fp.scale.z = 0;
    fp.updateAnimTransform();
  }
  viewer.impl.invalidate(true, true, true);
}

function showFrags(fragIds) {
  for (const fragId of fragIds) {
    const fp = viewer.impl.getFragmentProxy(viewer.model, fragId);
    fp.getAnimTransform();
    fp.scale.x = 1; fp.scale.y = 1; fp.scale.z = 1;
    fp.updateAnimTransform();
  }
  viewer.impl.invalidate(true, true, true);
}

// Click-to-remove flow — mirrors startRelocate() so the user clicks the exact
// asset they want to remove rather than whatever group the selection falls on.
function startRemoveAsset() {
  endRelocate();
  removeMode = true;
  setStatus("Remove: click an asset to remove it  (Esc to cancel)");
  viewer.container.addEventListener("click", onRemoveClick, true);
  document.addEventListener("keydown", onCancelRemove, true);
}

function onCancelRemove(ev) {
  if (ev.key !== "Escape") return;
  endRemoveMode();
  setStatus("remove cancelled", 1500);
}

function endRemoveMode() {
  removeMode = false;
  viewer.container && viewer.container.removeEventListener("click", onRemoveClick, true);
  document.removeEventListener("keydown", onCancelRemove, true);
}

function onRemoveClick(ev) {
  ev.stopPropagation(); ev.preventDefault();
  const hit = viewer.impl.hitTest(ev.clientX, ev.clientY, false);
  const name = assetNameAt(hit);
  if (!name) { setStatus("Not a removable asset — click a car, tree or infrastructure item", 2500); return; }
  endRemoveMode();
  const fragIds = fragsForAsset(name).slice();
  if (fragIds.length) { hideFrags(fragIds); removed[name] = fragIds; }
  const reg = assetByName[name];
  const assetType = reg ? reg.type : name.replace(/_\d+$/, "").toLowerCase();
  postEdit({ op: "remove", target: name, asset_type: assetType }, `removed ${name}`);
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
let relocateRoad = null;   // {road, grab:{x,y}} when a street is picked to relocate
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
  let drag = null;        // asset drag (fragment cluster)
  let roadDrag = null;    // street drag (overlay ribbon)
  const ground = ev => viewer.impl.intersectGround(ev.clientX, ev.clientY);
  const pickEv = ev => pickAt(viewer.impl.hitTest(ev.canvasX, ev.canvasY, false));
  // World point under a tool event — surface hit if any, else the ground plane.
  const worldAt = ev => {
    const h = viewer.impl.hitTest(ev.canvasX, ev.canvasY, false);
    return (h && (h.intersectPoint || h.point)) || ground(ev);
  };

  dragTool = {
    getNames: () => ["sf-drag"],
    getName: () => "sf-drag",
    getPriority: () => 100,            // above the orbit/navigation tools
    activate: () => {},
    deactivate: () => { drag = null; roadDrag = null; },

    handleButtonDown: (ev, button) => {
      if (relocateMode || roadBuildMode || removeMode || greenBuildMode) return false;
      if (button !== 0 || ev.shiftKey || ev.ctrlKey || ev.altKey || ev.metaKey) return false;
      const pick = pickEv(ev);
      const g = pick && ground(ev);
      if (pick && g) {                 // an asset (car/tree/button) takes priority
        drag = {
          name: pick.name, fragIds: pick.fragIds,
          fromUtm: placed[pick.name] || pick.reg.utm,
          startGround: { x: g.x, y: g.y },
          startOff: currentOffset(pick.name, pick.reg),
        };
        drag.lastOff = drag.startOff;
        viewer.container.style.cursor = "grabbing";
        setStatus(`Dragging "${pick.name}" — release to place`);
        return true;                   // consume so the camera doesn't move
      }
      const w = worldAt(ev);
      const r = w && roadAt(w);         // no asset -> maybe a street under the cursor
      if (r) {
        roadDrag = { road: r, startGround: { x: w.x, y: w.y }, last: { x: 0, y: 0 } };
        viewer.container.style.cursor = "grabbing";
        setStatus(`Dragging "${r.name}" — release to place`);
        return true;
      }
      return false;                    // empty ground -> let the camera orbit
    },

    handleMouseMove: ev => {
      if (drag) {
        const g = ground(ev);
        if (g) {                        // keep the grab point under the cursor
          const dx = drag.startOff.x + (g.x - drag.startGround.x);
          const dy = drag.startOff.y + (g.y - drag.startGround.y);
          moveFrags(drag.fragIds, dx, dy);
          drag.lastOff = { x: dx, y: dy };
        }
        return true;
      }
      if (roadDrag) {                   // slide the whole ribbon (re-drapes on drop)
        const w = worldAt(ev);
        if (w) {
          roadDrag.last = { x: w.x - roadDrag.startGround.x, y: w.y - roadDrag.startGround.y };
          roadDrag.road.mesh.position.set(roadDrag.last.x, roadDrag.last.y, 0);
          viewer.impl.invalidate(true, true, true);
        }
        return true;
      }
      // hover: hint that something grabbable (asset or street) is under the cursor
      if (!relocateMode && !roadBuildMode && !removeMode && !greenBuildMode && viewer.container.style.cursor !== "grabbing") {
        const hit = viewer.impl.hitTest(ev.canvasX, ev.canvasY, false);
        const grab = assetNameAt(hit) ||
                     roadAt((hit && (hit.intersectPoint || hit.point)) || ground(ev));
        viewer.container.style.cursor = grab ? "grab" : "";
      }
      return false;
    },

    handleButtonUp: (ev, button) => {
      if (drag) {
        const d = drag; drag = null;
        viewer.container.style.cursor = "grab";
        commitMove(d.name, d.fragIds, d.fromUtm, d.lastOff);
        return true;
      }
      if (roadDrag) {
        const rd = roadDrag; roadDrag = null;
        viewer.container.style.cursor = "grab";
        commitRoadMove(rd.road, rd.last.x, rd.last.y);
        return true;
      }
      return false;
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
  relocateRoad = null;
  relocateMode = true;
  setStatus("Relocate: click an asset or road to pick it up  (Esc to cancel)");
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
  relocateRoad = null;
  viewer.container.removeEventListener("click", onRelocateClick, true);
  document.removeEventListener("keydown", onCancelRelocate, true);
}

// First click picks the asset under the cursor; second click places it on the
// ground. (Container is full-window, so client coords == canvas coords.)
function onRelocateClick(ev) {
  ev.stopPropagation(); ev.preventDefault();
  // second click for a previously-picked street: shift it by (click - grab)
  if (relocateRoad) {
    const w = surfacePointAt(ev.clientX, ev.clientY);
    if (!w) { setStatus("placement failed — click on the ground", 2500); return; }
    const r = relocateRoad.road, g0 = relocateRoad.grab;
    endRelocate();
    commitRoadMove(r, w.x - g0.x, w.y - g0.y);
    return;
  }
  if (!relocatePick) {
    const pick = pickAt(viewer.impl.hitTest(ev.clientX, ev.clientY, false));
    if (pick) {
      relocatePick = { ...pick, fromUtm: placed[pick.name] || pick.reg.utm };
      setStatus(`Picked "${pick.name}" — click its destination  (Esc to cancel)`);
      return;
    }
    const w = surfacePointAt(ev.clientX, ev.clientY);
    const r = w && roadAt(w);
    if (r) {
      relocateRoad = { road: r, grab: { x: w.x, y: w.y } };
      setStatus(`Picked "${r.name}" — click its destination  (Esc to cancel)`);
      return;
    }
    setStatus("Not movable — click a car, tree, push-button or road", 2500);
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

function postEdit(edit, doneMsg) {
  fetch("/api/scenario?scene=" + encodeURIComponent(currentScene.id), {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ edit }),
  }).then(r => r.json()).then(res => {
    renderCost(res.estimate);
    const w = res.warnings || [];
    setStatus(w.length ? "⚠ " + w.join("; ") : (doneMsg || `moved ${edit.target}`),
              w.length ? 6000 : 2500);
  }).catch(err => { setStatus("save failed", 3000); console.error(err); });
}

function undoEdit() {
  fetch("/api/scenario?scene=" + encodeURIComponent(currentScene.id), {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ undo: true }),
  }).then(r => r.json()).then(res => {
    renderCost(res.estimate);
    const u = res.undone;
    if (u && u.op === "add_road") {
      removeRoadByName(u.target);
    } else if (u && u.op === "move_road") {
      redrawRoadAt(u.target, u.from_path_utm, u.width_m);
    } else if (u && u.op === "add_green") {
      removeGreenByName(u.target);
    } else if (u && u.op === "remove") {
      const fragIds = removed[u.target];
      if (fragIds) { showFrags(fragIds); delete removed[u.target]; }
    } else {
      const h = history.pop();
      if (h && assetByName[h.name]) {
        const reg = assetByName[h.name];
        placed[h.name] = h.from;
        const fragIds = (h.fragIds && h.fragIds.length) ? h.fragIds : fragsForAsset(h.name);
        moveFrags(fragIds, h.from[0] - reg.utm[0], h.from[1] - reg.utm[1]);
      }
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

// --- road building: extend roads across the scene, Cities:Skylines-style -----
// Two clicks place a straight segment. The ribbon is draped over the ground it
// crosses — subdivided along its length, each cross-section's height sampled from
// the surface beneath it — so it blankets the terrain/road rather than floating,
// and meets the existing road flush. Width is taken from the toolbar field
// (metres). Segments render as dark-asphalt overlay ribbons (they are NOT part of
// the SVF2 model) and persist as `add_road` edits in the scenario, which the cost
// engine prices (pavement area + curb both sides).

// World <-> UTM (the scenario's canonical frame). XY uses the scene origin and
// the model's global offset; Z uses the offset only (origin is 2-D).
function utmToWorld(u) {
  const o = (sceneMeta && sceneMeta.origin) || [0, 0];
  const off = (viewer.model.getData().globalOffset) || { x: 0, y: 0, z: 0 };
  return { x: u[0] - o[0] - off.x, y: u[1] - (o[1] || 0) - off.y, z: (u[2] || 0) - (off.z || 0) };
}
function worldToUtm(p) {
  const o = (sceneMeta && sceneMeta.origin) || [0, 0];
  const off = (viewer.model.getData().globalOffset) || { x: 0, y: 0, z: 0 };
  return [p.x + off.x + o[0], p.y + off.y + (o[1] || 0), p.z + (off.z || 0)];
}

// World-space point on the surface under a client pixel — the real mesh Z if a
// surface is hit (so endpoints sit at road height), else the ground plane.
function surfacePointAt(clientX, clientY) {
  const h = viewer.impl.hitTest(clientX, clientY, false);
  if (h && (h.intersectPoint || h.point)) return h.intersectPoint || h.point;
  return viewer.impl.intersectGround(clientX, clientY);
}

function horizLength(a, b) { return Math.hypot(b.x - a.x, b.y - a.y); }

// Highest point of the model + margin — the ray origin for downward sampling.
function modelTopZ() {
  try { return viewer.model.getBoundingBox().max.z + 10; } catch (e) { return 1e4; }
}

// Surface elevation (world Z) directly under a world XY, by casting a ray
// straight down against the model (world is Z-up). Returns `fallback` if nothing
// is hit or the ray API is unavailable on this viewer build — so roads still
// draw, just flat between their endpoints rather than draped.
function surfaceZAt(wx, wy, topZ, fallback) {
  if (!viewer.impl || typeof viewer.impl.rayIntersect !== "function") return fallback;
  let hit = null;
  try {
    const ray = new THREE.Ray(new THREE.Vector3(wx, wy, topZ),
                              new THREE.Vector3(0, 0, -1));
    hit = viewer.impl.rayIntersect(ray, true);   // ignoreTransparent
  } catch (e) { hit = null; }
  const p = hit && (hit.intersectPoint || hit.point);
  return p ? p.z : fallback;
}

// A road ribbon between two world points, `width` metres wide. With draping
// (opts.drape, the default for committed roads) the ribbon is subdivided along
// its length and each cross-section's left/right Z is sampled from the surface
// underneath, so the road blankets the ground it crosses. opts.drape:false makes
// a single sloped quad (cheap — used for the live aiming preview). Lifted
// ROAD_LIFT above the surface to avoid z-fighting.
function buildRoadMesh(s, e, width, opts) {
  opts = opts || {};
  const opacity = opts.opacity != null ? opts.opacity : 1;
  const drape = opts.drape !== false;
  const dx = e.x - s.x, dy = e.y - s.y;
  const len = Math.hypot(dx, dy) || 1e-6;
  const nx = -dy / len, ny = dx / len;         // unit perpendicular in XY
  const hw = width / 2;
  const step = opts.step || 1.5;               // metres between cross-sections
  const nSeg = drape ? Math.max(1, Math.min(Math.round(len / step), 240)) : 1;
  const topZ = opts.topZ != null ? opts.topZ : modelTopZ();

  const verts = [];
  let pL = null, pR = null;                    // previous cross-section corners
  for (let i = 0; i <= nSeg; i++) {
    const t = i / nSeg;
    const cx = s.x + dx * t, cy = s.y + dy * t;
    const fallZ = s.z + (e.z - s.z) * t;       // interpolated endpoint Z
    const lx = cx + nx * hw, ly = cy + ny * hw;
    const rx = cx - nx * hw, ry = cy - ny * hw;
    const lz = (drape ? surfaceZAt(lx, ly, topZ, fallZ) : fallZ) + ROAD_LIFT;
    const rz = (drape ? surfaceZAt(rx, ry, topZ, fallZ) : fallZ) + ROAD_LIFT;
    const Lp = [lx, ly, lz], Rp = [rx, ry, rz];
    if (pL) verts.push(...pL, ...pR, ...Rp, ...pL, ...Rp, ...Lp);  // bridge to prev
    pL = Lp; pR = Rp;
  }
  const geom = new THREE.BufferGeometry();
  geom.addAttribute("position", new THREE.BufferAttribute(new Float32Array(verts), 3));
  geom.computeBoundingBox();
  geom.computeBoundingSphere();
  const mat = new THREE.MeshBasicMaterial({
    color: ROAD_COLOR, side: THREE.DoubleSide,
    transparent: opacity < 1, opacity,
  });
  return new THREE.Mesh(geom, mat);
}

function ensureRoadOverlay() {
  if (roadOverlayReady) return;
  viewer.impl.createOverlayScene("roads");
  roadOverlayReady = true;
}

// Place a committed road overlay (no persistence — callers persist separately).
function addRoadOverlay(name, s, e, width) {
  ensureRoadOverlay();
  const mesh = buildRoadMesh(s, e, width, { drape: true });
  viewer.impl.addOverlay("roads", mesh);
  roads.push({ name, mesh, s: { x: s.x, y: s.y, z: s.z },
               e: { x: e.x, y: e.y, z: e.z }, width });
  viewer.impl.invalidate(true, true, true);
  return mesh;
}

function removeRoadByName(name) {
  const i = roads.findIndex(r => r.name === name);
  if (i < 0) return;
  if (roadOverlayReady) viewer.impl.removeOverlay("roads", roads[i].mesh);
  roads.splice(i, 1);
  viewer.impl.invalidate(true, true, true);
}

function clearRoads() {
  if (roadOverlayReady) {
    for (const r of roads) viewer.impl.removeOverlay("roads", r.mesh);
    viewer.impl.removeOverlayScene("roads");
  }
  roads = []; roadOverlayReady = false; roadSeq = 0;
  roadStart = null; roadPreview = null;
}

function clearRoadPreview() {
  if (roadPreview && roadOverlayReady) viewer.impl.removeOverlay("roads", roadPreview);
  roadPreview = null;
}

function roadWidth() {
  const el = document.getElementById("road-width");
  const w = el ? parseFloat(el.value) : 7;
  return (isFinite(w) && w > 0) ? w : 7;
}

// The road whose footprint (straight ribbon, `width` wide) contains a world XY,
// topmost first, or null. Overlay ribbons aren't returned by hitTest, so the
// relocate/drag tools grab a street by testing its 2-D footprint instead.
function roadAt(pt) {
  if (!pt) return null;
  for (let i = roads.length - 1; i >= 0; i--) {
    const r = roads[i];
    if (!r.s || !r.e) continue;
    const dx = r.e.x - r.s.x, dy = r.e.y - r.s.y;
    const len2 = dx * dx + dy * dy || 1e-9;
    let t = ((pt.x - r.s.x) * dx + (pt.y - r.s.y) * dy) / len2;
    t = Math.max(0, Math.min(1, t));
    const d = Math.hypot(pt.x - (r.s.x + dx * t), pt.y - (r.s.y + dy * t));
    if (d <= r.width / 2 + 0.5) return r;          // 0.5 m grab tolerance
  }
  return null;
}

// Re-drape a road at endpoints shifted by (dx,dy) and persist the move. Moving a
// street costs nothing (same pavement) — `move_road` adds no cost line item.
function commitRoadMove(r, dx, dy) {
  r.mesh.position.set(0, 0, 0);                    // drop any live drag offset
  if (Math.hypot(dx, dy) < 0.05) { viewer.impl.invalidate(true, true, true); return; }
  const fromPath = [worldToUtm(r.s), worldToUtm(r.e)];
  const top = modelTopZ();
  const ns = { x: r.s.x + dx, y: r.s.y + dy, z: 0 };
  const ne = { x: r.e.x + dx, y: r.e.y + dy, z: 0 };
  ns.z = surfaceZAt(ns.x, ns.y, top, r.s.z);
  ne.z = surfaceZAt(ne.x, ne.y, top, r.e.z);
  if (roadOverlayReady) viewer.impl.removeOverlay("roads", r.mesh);
  r.mesh = buildRoadMesh(ns, ne, r.width, { drape: true });
  viewer.impl.addOverlay("roads", r.mesh);
  r.s = ns; r.e = ne;
  viewer.impl.invalidate(true, true, true);
  postEdit({ op: "move_road", target: r.name, asset_type: "road",
             from_path_utm: fromPath, to_path_utm: [worldToUtm(ns), worldToUtm(ne)],
             width_m: r.width, length_m: horizLength(ns, ne) }, `moved ${r.name}`);
}

// Remove a road and redraw it (draped) at a UTM path — used to revert a move on undo.
function redrawRoadAt(name, path_utm, width) {
  if (!path_utm || path_utm.length < 2) return;
  removeRoadByName(name);
  addRoadOverlay(name, utmToWorld(path_utm[0]),
                 utmToWorld(path_utm[path_utm.length - 1]), width || 7);
}

// Redraw persisted roads for the current scene from its scenario.
function loadRoads() {
  return fetch("/api/scenario?scene=" + encodeURIComponent(currentScene.id))
    .then(r => r.json()).then(res => {
      const edits = (res.scenario && res.scenario.edits) || [];
      edits.filter(e => e.op === "add_road" && e.path_utm && e.path_utm.length >= 2)
        .forEach(e => {
          let path = e.path_utm;                   // current spot = last move, else add
          for (const mv of edits)
            if (mv.op === "move_road" && mv.target === e.target && mv.to_path_utm)
              path = mv.to_path_utm;
          addRoadOverlay(e.target, utmToWorld(path[0]),
                         utmToWorld(path[path.length - 1]), e.width_m || 7);
          const m = /_(\d+)$/.exec(e.target || "");
          if (m) roadSeq = Math.max(roadSeq, parseInt(m[1], 10));
        });
    }).catch(() => {});
}

// --- road tool: click start, click end ---------------------------------------

function startBuildRoad() {
  if (!viewer.model || !sceneMeta) return setStatus("scene still loading…", 2000);
  endRelocate();
  endBuildGreen();
  roadBuildMode = true;
  roadStart = null;
  clearRoadPreview();
  ensureRoadOverlay();
  viewer.container.style.cursor = "crosshair";
  setStatus(`Build road (${roadWidth()} m wide): click the start point  (Esc to finish)`);
  viewer.container.addEventListener("click", onBuildRoadClick, true);
  viewer.container.addEventListener("mousemove", onBuildRoadMove, true);
  document.addEventListener("keydown", onCancelBuildRoad, true);
}

function endBuildRoad() {
  roadBuildMode = false;
  roadStart = null;
  clearRoadPreview();
  if (viewer && viewer.container) viewer.container.style.cursor = "";
  viewer.container && viewer.container.removeEventListener("click", onBuildRoadClick, true);
  viewer.container && viewer.container.removeEventListener("mousemove", onBuildRoadMove, true);
  document.removeEventListener("keydown", onCancelBuildRoad, true);
}

function onCancelBuildRoad(ev) {
  if (ev.key !== "Escape") return;
  endBuildRoad();
  setStatus("road tool closed", 1500);
}

function onBuildRoadClick(ev) {
  ev.stopPropagation(); ev.preventDefault();
  const pt = surfacePointAt(ev.clientX, ev.clientY);
  if (!pt) { setStatus("click on the ground or a road surface", 2500); return; }
  if (!roadStart) {                       // first click: anchor the start
    roadStart = { x: pt.x, y: pt.y, z: pt.z };
    setStatus(`Start set — click the end point  (${roadWidth()} m wide, Esc to finish)`);
    return;
  }
  const s = roadStart, e = { x: pt.x, y: pt.y, z: pt.z };
  const width = roadWidth();
  clearRoadPreview();
  roadStart = null;                       // ready for the next independent segment
  if (horizLength(s, e) < 0.5) {          // ignore an accidental double-click
    setStatus("segment too short — click a start point", 2000);
    return;
  }
  roadSeq += 1;
  const name = "road_" + String(roadSeq).padStart(2, "0");
  addRoadOverlay(name, s, e, width);
  const path_utm = [worldToUtm(s), worldToUtm(e)];
  postEdit({ op: "add_road", target: name, asset_type: "road",
             path_utm, width_m: width, length_m: horizLength(s, e) },
           `built ${name} (${horizLength(s, e).toFixed(1)} m × ${width} m)`);
  setStatus(`built ${name} — click another start point  (Esc to finish)`);
}

// Rubber-band preview between the anchored start and the cursor.
function onBuildRoadMove(ev) {
  if (!roadBuildMode || !roadStart) return;
  const pt = surfacePointAt(ev.clientX, ev.clientY);
  if (!pt) return;
  clearRoadPreview();
  roadPreview = buildRoadMesh(roadStart, { x: pt.x, y: pt.y, z: pt.z }, roadWidth(), { opacity: 0.45, drape: false });
  viewer.impl.addOverlay("roads", roadPreview);
  viewer.impl.invalidate(true, true, true);
}

// --- green terrain fill tool -------------------------------------------------
// Two clicks define opposite corners of a rectangle. The patch is draped over
// the surface (same ray-sampling as roads) and rendered above roads (GREEN_LIFT
// > ROAD_LIFT) so it blankets existing asphalt when painted over a road.
// Persists as `add_green` edits priced by area (topsoil + sod per sq ft).

function buildGreenMesh(s, e, opts) {
  opts = opts || {};
  const opacity = opts.opacity != null ? opts.opacity : 1;
  const drape = opts.drape !== false;
  const topZ = opts.topZ != null ? opts.topZ : modelTopZ();
  const step = 2.0;
  const dx = e.x - s.x, dy = e.y - s.y;
  const nX = drape ? Math.max(1, Math.round(Math.abs(dx) / step)) : 1;
  const nY = drape ? Math.max(1, Math.round(Math.abs(dy) / step)) : 1;
  const fallZ = (s.z + e.z) / 2;

  function cellZ(x, y) {
    if (drape) return surfaceZAt(x, y, topZ, fallZ) + GREEN_LIFT;
    // For the live preview, interpolate endpoint Z — no ray casting needed.
    const tx = dx ? (x - s.x) / dx : 0.5;
    const ty = dy ? (y - s.y) / dy : 0.5;
    return s.z + (e.z - s.z) * (tx * 0.5 + ty * 0.5) + GREEN_LIFT;
  }

  const verts = [];
  for (let j = 0; j < nY; j++) {
    for (let i = 0; i < nX; i++) {
      const x0 = s.x + dx * (i / nX),       y0 = s.y + dy * (j / nY);
      const x1 = s.x + dx * ((i + 1) / nX), y1 = s.y + dy * ((j + 1) / nY);
      const z00 = cellZ(x0, y0), z10 = cellZ(x1, y0);
      const z01 = cellZ(x0, y1), z11 = cellZ(x1, y1);
      verts.push(x0, y0, z00,  x1, y0, z10,  x1, y1, z11);
      verts.push(x0, y0, z00,  x1, y1, z11,  x0, y1, z01);
    }
  }
  const geom = new THREE.BufferGeometry();
  geom.addAttribute("position", new THREE.BufferAttribute(new Float32Array(verts), 3));
  geom.computeBoundingBox();
  geom.computeBoundingSphere();
  const mat = new THREE.MeshBasicMaterial({
    color: GREEN_COLOR, side: THREE.DoubleSide,
    transparent: opacity < 1, opacity,
  });
  return new THREE.Mesh(geom, mat);
}

function ensureGreenOverlay() {
  if (greenOverlayReady) return;
  viewer.impl.createOverlayScene("greens");
  greenOverlayReady = true;
}

function addGreenOverlay(name, s, e) {
  ensureGreenOverlay();
  const mesh = buildGreenMesh(s, e, { drape: true });
  viewer.impl.addOverlay("greens", mesh);
  greens.push({ name, mesh, s, e });
  viewer.impl.invalidate(true, true, true);
  return mesh;
}

function removeGreenByName(name) {
  const i = greens.findIndex(g => g.name === name);
  if (i < 0) return;
  if (greenOverlayReady) viewer.impl.removeOverlay("greens", greens[i].mesh);
  greens.splice(i, 1);
  viewer.impl.invalidate(true, true, true);
}

function clearGreens() {
  if (greenOverlayReady) {
    for (const g of greens) viewer.impl.removeOverlay("greens", g.mesh);
    viewer.impl.removeOverlayScene("greens");
  }
  greens = []; greenOverlayReady = false; greenSeq = 0;
  greenStart = null; greenPreview = null;
}

function clearGreenPreview() {
  if (greenPreview && greenOverlayReady) viewer.impl.removeOverlay("greens", greenPreview);
  greenPreview = null;
}

// Restore persisted green patches from the scenario on scene load.
function loadGreens() {
  return fetch("/api/scenario?scene=" + encodeURIComponent(currentScene.id))
    .then(r => r.json()).then(res => {
      const edits = (res.scenario && res.scenario.edits) || [];
      edits.filter(e => e.op === "add_green" && e.start_utm && e.end_utm)
        .forEach(e => {
          const s = utmToWorld(e.start_utm), en = utmToWorld(e.end_utm);
          addGreenOverlay(e.target, s, en);
          const m = /_(\d+)$/.exec(e.target || "");
          if (m) greenSeq = Math.max(greenSeq, parseInt(m[1], 10));
        });
    }).catch(() => {});
}

function startBuildGreen() {
  if (!viewer.model || !sceneMeta) return setStatus("scene still loading…", 2000);
  endRelocate();
  endBuildRoad();
  greenBuildMode = true;
  greenStart = null;
  clearGreenPreview();
  ensureGreenOverlay();
  viewer.container.style.cursor = "crosshair";
  setStatus("Fill green: click the first corner  (Esc to cancel)");
  viewer.container.addEventListener("click", onBuildGreenClick, true);
  viewer.container.addEventListener("mousemove", onBuildGreenMove, true);
  document.addEventListener("keydown", onCancelBuildGreen, true);
}

function endBuildGreen() {
  greenBuildMode = false;
  greenStart = null;
  clearGreenPreview();
  if (viewer && viewer.container) viewer.container.style.cursor = "";
  viewer.container && viewer.container.removeEventListener("click", onBuildGreenClick, true);
  viewer.container && viewer.container.removeEventListener("mousemove", onBuildGreenMove, true);
  document.removeEventListener("keydown", onCancelBuildGreen, true);
}

function onCancelBuildGreen(ev) {
  if (ev.key !== "Escape") return;
  endBuildGreen();
  setStatus("green tool closed", 1500);
}

function onBuildGreenClick(ev) {
  ev.stopPropagation(); ev.preventDefault();
  const pt = surfacePointAt(ev.clientX, ev.clientY);
  if (!pt) { setStatus("click on a surface", 2500); return; }
  if (!greenStart) {
    greenStart = { x: pt.x, y: pt.y, z: pt.z };
    setStatus("First corner set — click the opposite corner  (Esc to cancel)");
    return;
  }
  const s = greenStart, e = { x: pt.x, y: pt.y, z: pt.z };
  clearGreenPreview();
  greenStart = null;
  const areaSqm = Math.abs((e.x - s.x) * (e.y - s.y));
  if (areaSqm < 0.25) { setStatus("area too small — click a first corner", 2000); return; }
  greenSeq += 1;
  const name = "green_" + String(greenSeq).padStart(2, "0");
  addGreenOverlay(name, s, e);
  const areaSqft = areaSqm * 10.763910416709722;
  postEdit({ op: "add_green", target: name, asset_type: "green",
             start_utm: worldToUtm(s), end_utm: worldToUtm(e),
             area_sqft: areaSqft },
           `placed ${name} (${areaSqm.toFixed(0)} m²)`);
  setStatus(`placed ${name} — click another first corner  (Esc to finish)`);
}

function onBuildGreenMove(ev) {
  if (!greenBuildMode || !greenStart) return;
  const pt = surfacePointAt(ev.clientX, ev.clientY);
  if (!pt) return;
  clearGreenPreview();
  greenPreview = buildGreenMesh(greenStart, { x: pt.x, y: pt.y, z: pt.z },
                                { opacity: 0.4, drape: false });
  viewer.impl.addOverlay("greens", greenPreview);
  viewer.impl.invalidate(true, true, true);
}

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
  const usd = n => "$" + Number(n || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const total = (rep && !rep.error) ? (rep.total || 0) : 0;
  const budgetEl = document.getElementById("budget-total");
  if (budgetEl) budgetEl.textContent = usd(total);

  const body = document.getElementById("cost-body");
  if (!body) return;
  if (!rep || rep.error) { body.textContent = (rep && rep.error) || "no estimate"; return; }
  if (!rep.line_items || !rep.line_items.length) { body.textContent = "No edits yet."; return; }
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

// Reset scene: clear all edits on the server, restore all moved/removed objects
// in the viewer, and zero out the budget display.
function resetScene() {
  fetch("/api/scenario?scene=" + encodeURIComponent(currentScene.id), {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ clear: true }),
  }).then(r => r.json()).then(res => {
    for (const name in removed) showFrags(removed[name]);
    removed = {};
    for (const name in placed) {
      const reg = assetByName[name];
      if (reg) moveFrags(fragsForAsset(name), 0, 0);
    }
    placed = {};
    history = [];
    clearRoads();
    clearGreens();
    renderCost(res.estimate);
    setStatus("Scene reset to original state", 2500);
  }).catch(err => { setStatus("reset failed", 3000); console.error(err); });
}

