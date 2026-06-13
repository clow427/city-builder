let viewer;
let selected = null;
let lidarPoints = null;
let lidarVisible = false;   // start with clean CAD; "Toggle point cloud" shows raw scan

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
      id => { if (tree.getChildCount(id) === 0) leaves.push(tree.getNodeName(id) || id); },
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
      .then(setHumanView);
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

function moveCar() {
  if (selected == null) return alert("Select a car first");
  const tree = viewer.model.getInstanceTree();
  tree.enumNodeFragments(selected, fragId => {
    const fp = viewer.impl.getFragmentProxy(viewer.model, fragId);
    fp.getAnimTransform();
    fp.position.x += 5;            // slide 5 m along X
    fp.updateAnimTransform();
  });
  viewer.impl.invalidate(true, true, true);
}

function deleteCar() {
  if (selected == null) return alert("Select a car first");
  viewer.hide(selected);
}
