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

Autodesk.Viewing.Initializer({
  env: "AutodeskProduction2", api: "streamingV2",
  getAccessToken: cb => fetch("/api/token").then(r => r.json())
    .then(t => cb(t.access_token, t.expires_in)),
}, () => {
  viewer = new Autodesk.Viewing.GuiViewer3D(document.getElementById("v"));
  viewer.start();
  viewer.addEventListener(Autodesk.Viewing.SELECTION_CHANGED_EVENT,
    e => selected = e.dbIdArray[0] ?? null);
  viewer.addEventListener(Autodesk.Viewing.OBJECT_TREE_CREATED_EVENT, () => {
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
    b.style.cssText = "position:absolute;z-index:9;top:8px;right:8px;background:#DEFF00;padding:4px 8px;font:700 13px sans-serif";
    b.textContent = "objects loaded: " + leaves.length + " [" + leaves.join(", ") + "]";
    document.body.appendChild(b);
    console.log("leaf objects:", leaves);
  });
  Autodesk.Viewing.Document.load("urn:" + URN, doc => {
    // useConsolidation:false — consolidation merges small meshes into shared
    // GPU batches, which makes per-object fragment transforms drag neighbors.
    viewer.loadDocumentNode(doc, doc.getRoot().getDefaultGeometry(),
        { useConsolidation: false })
      .then(loadPointCloud)
      .then(setHumanView)
      .then(loadSceneData)
      .then(refreshCost);
  }, err => console.error("load failed", err));
});

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
  fetch("/points.bin?v=3").then(r => r.arrayBuffer()).then(buf => {
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
  return Promise.all([
    fetch("/api/scene_meta").then(r => r.json()).then(m => { sceneMeta = m; }).catch(() => {}),
    fetch("/api/assets").then(r => r.json())
      .then(a => (a || []).forEach(x => { assetByName[x.name] = x; })).catch(() => {}),
  ]);
}

function setStatus(msg, ms) {
  const el = document.getElementById("status");
  if (!msg) { el.style.display = "none"; return; }
  el.textContent = msg; el.style.display = "inline";
  if (ms) setTimeout(() => { el.style.display = "none"; }, ms);
}

function selectedName() {
  if (selected == null) return null;
  const tree = viewer.model.getInstanceTree();
  return tree ? tree.getNodeName(selected) : null;
}

// --- relocation: click an asset, then click the ground to place it -----------

function startRelocate() {
  const name = selectedName();
  if (name == null) return alert("Select an asset first");
  if (!assetByName[name]) return alert(`"${name}" is not a draggable asset`);
  relocateMode = true;
  setStatus(`Click the ground to place "${name}"  (Esc to cancel)`);
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
  viewer.container.removeEventListener("click", onPlace, true);
  document.removeEventListener("keydown", onCancelRelocate, true);
}

// Convert a clicked ground point (viewer world coords) to UTM, move the
// selected asset's fragment there, persist the relocate edit, and re-price.
function onPlace(ev) {
  ev.stopPropagation(); ev.preventDefault();
  const name = selectedName(), reg = assetByName[name];
  endRelocate();
  const g = viewer.impl.intersectGround(ev.clientX, ev.clientY);
  if (!g || !reg || !sceneMeta) { setStatus("placement failed", 2500); return; }

  // model space = world + globalOffset (the viewer subtracts it for rendering);
  // UTM = model-local + scene origin. Deltas are translation-invariant, so we
  // work entirely in UTM. (placementWithOffset rotation, if any, is ignored.)
  const off = (viewer.model.getData().globalOffset) || { x: 0, y: 0 };
  const o = sceneMeta.origin || [0, 0];
  const toUtm = [g.x + off.x + o[0], g.y + off.y + o[1], reg.utm[2]];
  const fromUtm = placed[name] || reg.utm;

  moveFragmentTo(selected, toUtm[0] - reg.utm[0], toUtm[1] - reg.utm[1]);
  history.push({ name, from: fromUtm });
  placed[name] = toUtm;
  postEdit({ op: "relocate", target: name, asset_type: reg.type,
             from_utm: fromUtm, to_utm: toUtm });
}

function moveFragmentTo(dbId, dx, dy) {
  const tree = viewer.model.getInstanceTree();
  tree.enumNodeFragments(dbId, fragId => {
    const fp = viewer.impl.getFragmentProxy(viewer.model, fragId);
    fp.getAnimTransform();
    fp.position.x = dx; fp.position.y = dy;   // offset from authored anchor
    fp.updateAnimTransform();
  });
  viewer.impl.invalidate(true, true, true);
}

function postEdit(edit) {
  fetch("/api/scenario", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ edit }),
  }).then(r => r.json()).then(res => {
    renderCost(res.estimate);
    const w = res.warnings || [];
    setStatus(w.length ? "⚠ " + w.join("; ") : `moved ${edit.target}`, w.length ? 6000 : 2500);
  }).catch(err => { setStatus("save failed", 3000); console.error(err); });
}

function undoEdit() {
  fetch("/api/scenario", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ undo: true }),
  }).then(r => r.json()).then(res => {
    renderCost(res.estimate);
    const h = history.pop();
    if (h && assetByName[h.name] && nameDb[h.name] != null) {
      const reg = assetByName[h.name];
      placed[h.name] = h.from;
      moveFragmentTo(nameDb[h.name], h.from[0] - reg.utm[0], h.from[1] - reg.utm[1]);
    }
    setStatus("undid last edit", 2000);
  }).catch(err => { setStatus("undo failed", 3000); console.error(err); });
}

// --- live cost panel (running total of the proposal) -------------------------

function toggleCost() {
  const panel = document.getElementById("cost");
  const show = panel.style.display === "none";
  panel.style.display = show ? "block" : "none";
  if (show) refreshCost();
}

function refreshCost() {
  return fetch("/api/scenario").then(r => r.json())
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
