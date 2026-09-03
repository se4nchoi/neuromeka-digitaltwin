/* ==========================================================================
   NEUROMEKA INDY7 3D DIGITAL TWIN & MULTI-PURPOSE CENTER
   - Exact Craig MDH Forward Kinematics
   - True Indy7 Arm Geometry with 183mm Offset Wrist
   - Interactive Web Teach Pendant (Joint & Task Jogging)
   - Live Waypoint Acquisition, 3D Markers, & Sequence Runner
   - Multi-Program Center (Palletizing, Put-Back, Zero, Home, Direct Teach)
   - Real-Time Hardware Telemetry & I/O Tracking
   ========================================================================== */

(function () {
  "use strict";

  // --- CONFIGURATION & CALIBRATED WORKCELL POSITIONS ---
  const WORKCELL = {
    pickPos: [232.49, 514.57, 254.19, -19.52, -179.64, 90.03],
    dropBase: [201.75, 219.29, 304.94, -3.24, -179.44, 90.01],
    magInsert: [-8.15, 515.98, 343.32, -19.46, -177.65, 90.01],
    homeJoints: [0.0, 0.0, -90.0, 0.0, -90.0, 0.0],
    offsetX: 80.0,
    offsetY: 80.0,
    layerHeight: 30.0,
    slotsPerFloor: 4,
    numFloors: 2
  };

  // Exact Craig Modified Denavit-Hartenberg (MDH) Parameters for Neuromeka Indy7
  const MDH_PARAMS = [
    { alpha: 0.0,  a: 0.0,   theta0: 0.0,   d0: 300.0 }, // Joint 1: Base to J2 shoulder axis
    { alpha: 90.0, a: 0.0,   theta0: 90.0,  d0: 0.0 },   // Joint 2: Shoulder pitch
    { alpha: 0.0,  a: 450.0, theta0: 90.0,  d0: 3.5 },   // Joint 3: Elbow pitch (Upper arm a=450)
    { alpha: 90.0, a: 0.0,   theta0: 180.0, d0: 350.0 }, // Joint 4: Wrist 1 roll (Forearm d=350)
    { alpha: 90.0, a: 0.0,   theta0: 180.0, d0: 183.0 }, // Joint 5: Wrist 2 pitch (Transverse offset d=183)
    { alpha: 90.0, a: 0.0,   theta0: 180.0, d0: 228.0 }, // Joint 6: Tool Flange roll (Tool link d=228)
  ];

  // --- THREE.JS SCENE GLOBALS ---
  let scene, camera, renderer, controls;
  let ambientLight, hemiLight, dirLight, cyanLight, studioLight;
  let currentLightingIndex = 0;
  const LIGHTING_MODES = [
    { name: "BRIGHT", icon: "☀️", label: "☀️ LIGHT: BRIGHT (1.5x)", amb: 2.4, hemi: 1.6, dir: 3.0, studio: 1.5, expo: 1.35, fog: 0.00015 },
    { name: "ULTRA",  icon: "⚡", label: "⚡ LIGHT: ULTRA (2.0x)",  amb: 3.8, hemi: 2.4, dir: 4.2, studio: 2.5, expo: 1.65, fog: 0.00008 },
    { name: "MOODY",  icon: "🌙", label: "🌙 LIGHT: MOODY (1.0x)",  amb: 1.4, hemi: 0.9, dir: 2.0, studio: 0.6, expo: 1.10, fog: 0.00035 }
  ];

  // Robot Link Groups (Kinematic Chain)
  let robotBase;
  const jointGroups = [];
  let baseLedHalo;
  let jawLeft, jawRight, heldWorkpieceMesh;
  let feederGroup, feederStack = [];
  let palletGroup, palletMeshes = [];
  let trailGeometry, trailLine, trailPoints = [];
  const MAX_TRAIL_POINTS = 350;

  // 3D Waypoint Visualizer Markers
  let waypointMarkersGroup;
  let currentWaypointsList = [];

  // Telemetry & Kinematic State
  let currentQ = [0.0, 0.0, -90.0, 0.0, -90.0, 0.0];
  let targetQ  = [0.0, 0.0, -90.0, 0.0, -90.0, 0.0];
  let currentP = [350.0, -186.5, 522.0, 0.0, -180.0, 0.0];
  let gripperState = false;
  let heldWorkpiece = false;
  let lastServerState = null;
  let isHardwareConnected = false;
  let currentMode = "DISCONNECTED";
  let directTeachingState = false;

  // Teach Pendant State
  let activeJogMode = "joint"; // "joint" or "task"
  let currentJointStep = 1.0;  // deg
  let currentTaskStep = 5.0;   // mm
  let currentSpeedRatio = 25;  // %
  let continuousJogInterval = null;

  // Camera Animation Target
  let camTargetPos = null;
  let camTargetLook = null;

  // --- INITIALIZATION ---
  window.addEventListener("DOMContentLoaded", () => {
    init3D();
    initUI();
    initMultiCenter();
    initWebSocket();
    animate();
  });

  function init3D() {
    const container = document.getElementById("canvas3d");
    const width = container.clientWidth || window.innerWidth;
    const height = container.clientHeight || window.innerHeight;

    // 1. Scene & Fog
    scene = new THREE.Scene();
    scene.background = new THREE.Color(0x070a12);
    scene.fog = new THREE.FogExp2(0x070a12, 0.00015);

    // 2. Camera (Z-Up industrial coordinate system)
    camera = new THREE.PerspectiveCamera(45, width / height, 10, 15000);
    camera.position.set(1100, -1300, 1100);
    camera.up.set(0, 0, 1);

    // 3. Renderer
    renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "high-performance" });
    renderer.setSize(width, height);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.35;
    container.appendChild(renderer.domElement);

    // 4. Orbit Controls
    controls = new THREE.OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.06;
    controls.target.set(160, 320, 260);
    controls.maxPolarAngle = Math.PI / 2 + 0.05;
    controls.minDistance = 200;
    controls.maxDistance = 4500;

    // 5. Lighting Setup
    ambientLight = new THREE.AmbientLight(0x334155, 2.4);
    scene.add(ambientLight);

    hemiLight = new THREE.HemisphereLight(0x7dd3fc, 0x1e293b, 1.6);
    hemiLight.position.set(0, 0, 1500);
    scene.add(hemiLight);

    dirLight = new THREE.DirectionalLight(0xffffff, 3.0);
    dirLight.position.set(800, -600, 1500);
    dirLight.castShadow = true;
    dirLight.shadow.mapSize.width = 2048;
    dirLight.shadow.mapSize.height = 2048;
    dirLight.shadow.camera.near = 100;
    dirLight.shadow.camera.far = 3800;
    const d = 950;
    dirLight.shadow.camera.left = -d;
    dirLight.shadow.camera.right = d;
    dirLight.shadow.camera.top = d;
    dirLight.shadow.camera.bottom = -d;
    dirLight.shadow.bias = -0.0005;
    scene.add(dirLight);

    studioLight = new THREE.DirectionalLight(0xf8fafc, 1.5);
    studioLight.position.set(-400, 600, 1600);
    scene.add(studioLight);

    cyanLight = new THREE.PointLight(0x06b6d4, 2.5, 2000);
    cyanLight.position.set(-450, 650, 450);
    scene.add(cyanLight);

    applyLightingMode(0);

    // 6. Floor & Workcell Table
    createEnvironment();

    // 7. Workcell Props (Feeder & Pallet)
    createWorkcellProps();

    // 8. Procedural Neuromeka Indy7 Arm (Exact MDH Kinematics)
    createIndy7Robot();

    // 9. Trajectory Ribbon & Waypoint Markers
    createTrajectoryRibbon();

    waypointMarkersGroup = new THREE.Group();
    scene.add(waypointMarkersGroup);

    window.addEventListener("resize", onWindowResize);
  }

  // --- WORKCELL ENVIRONMENT ---
  function createEnvironment() {
    // Floor
    const floorGeo = new THREE.PlaneGeometry(7000, 7000);
    const floorMat = new THREE.MeshStandardMaterial({
      color: 0x060911,
      roughness: 0.9,
      metalness: 0.15
    });
    const floor = new THREE.Mesh(floorGeo, floorMat);
    floor.receiveShadow = true;
    scene.add(floor);

    // Cyan Industrial Coordinate Grid
    const grid = new THREE.GridHelper(3200, 64, 0x06b6d4, 0x1e293b);
    grid.rotation.x = Math.PI / 2;
    grid.position.z = 1;
    scene.add(grid);

    // Heavy Industrial Granite Worktable
    const tableGeo = new THREE.BoxGeometry(1150, 950, 45);
    const tableMat = new THREE.MeshStandardMaterial({
      color: 0x0f172a,
      roughness: 0.35,
      metalness: 0.65
    });
    const table = new THREE.Mesh(tableGeo, tableMat);
    table.position.set(160, 360, -22.5);
    table.receiveShadow = true;
    table.castShadow = true;
    scene.add(table);

    // Table Accent Border
    const frameGeo = new THREE.EdgesGeometry(tableGeo);
    const frameMat = new THREE.LineBasicMaterial({ color: 0x06b6d4, transparent: true, opacity: 0.45 });
    const tableFrame = new THREE.LineSegments(frameGeo, frameMat);
    table.add(tableFrame);
  }

  // --- CALIBRATED WORKCELL PROPS ---
  function createWorkcellProps() {
    // 1. Slanted Magazine Feeder (-19.52° angle)
    feederGroup = new THREE.Group();
    feederGroup.position.set(WORKCELL.pickPos[0], WORKCELL.pickPos[1], WORKCELL.pickPos[2] - 125);
    feederGroup.rotation.x = THREE.MathUtils.degToRad(-19.52);

    const fBaseMat = new THREE.MeshStandardMaterial({ color: 0x1e293b, roughness: 0.4, metalness: 0.7 });
    const fBase = new THREE.Mesh(new THREE.BoxGeometry(130, 110, 20), fBaseMat);
    fBase.castShadow = true;
    feederGroup.add(fBase);

    // Aluminum Guide Rails
    const railMat = new THREE.MeshStandardMaterial({ color: 0x475569, roughness: 0.25, metalness: 0.85 });
    const railL = new THREE.Mesh(new THREE.BoxGeometry(12, 85, 290), railMat);
    railL.position.set(-36, 0, 145);
    railL.castShadow = true;
    feederGroup.add(railL);

    const railR = new THREE.Mesh(new THREE.BoxGeometry(12, 85, 290), railMat);
    railR.position.set(36, 0, 145);
    railR.castShadow = true;
    feederGroup.add(railR);

    // Magazine Photoelectric Sensor (DI3)
    const sensorGeo = new THREE.BoxGeometry(18, 26, 18);
    const sensorMat = new THREE.MeshStandardMaterial({
      color: 0x10b981,
      emissive: 0x10b981,
      emissiveIntensity: 0.6
    });
    const sensorMesh = new THREE.Mesh(sensorGeo, sensorMat);
    sensorMesh.position.set(48, 0, 115);
    sensorMesh.name = "magSensorMesh";
    feederGroup.add(sensorMesh);

    // Stack of 8 Cylindrical Billets inside feeder
    const partMat = new THREE.MeshStandardMaterial({
      color: 0x06b6d4,
      roughness: 0.3,
      metalness: 0.5,
      emissive: 0x0891b2,
      emissiveIntensity: 0.25
    });
    for (let i = 0; i < 8; i++) {
      const p = new THREE.Mesh(new THREE.CylinderGeometry(24, 24, 28, 24), partMat);
      p.rotation.x = Math.PI / 2;
      p.position.set(0, 0, 115 + i * 32);
      p.castShadow = true;
      feederGroup.add(p);
      feederStack.push(p);
    }
    scene.add(feederGroup);

    // 2. 8-Slot 2-Floor Pallet Tray
    palletGroup = new THREE.Group();
    palletGroup.position.set(WORKCELL.dropBase[0] - 40, WORKCELL.dropBase[1] + 40, WORKCELL.dropBase[2] - 15);

    const palletBaseMat = new THREE.MeshStandardMaterial({
      color: 0x0f172a,
      roughness: 0.2,
      metalness: 0.85,
      transparent: true,
      opacity: 0.9
    });
    const palletPlate = new THREE.Mesh(new THREE.BoxGeometry(230, 230, 12), palletBaseMat);
    palletPlate.castShadow = true;
    palletPlate.receiveShadow = true;
    palletGroup.add(palletPlate);

    const pEdges = new THREE.LineSegments(
      new THREE.EdgesGeometry(palletPlate.geometry),
      new THREE.LineBasicMaterial({ color: 0x06b6d4, linewidth: 2 })
    );
    palletPlate.add(pEdges);

    // 8 Pallet Slot markers & Placed Meshes
    const boxGeo = new THREE.BoxGeometry(36, 36, 26);
    const boxPlacedMat = new THREE.MeshStandardMaterial({
      color: 0x10b981,
      roughness: 0.3,
      metalness: 0.6,
      emissive: 0x059669,
      emissiveIntensity: 0.35
    });

    for (let i = 0; i < 8; i++) {
      const layer = Math.floor(i / WORKCELL.slotsPerFloor);
      const slot = i % WORKCELL.slotsPerFloor;
      const r = Math.floor(slot / 2);
      const c = slot % 2;

      const posX = 40 - r * 80;
      const posY = -40 + c * 80;
      const posZ = 16 + layer * WORKCELL.layerHeight;

      const slotWire = new THREE.LineSegments(
        new THREE.EdgesGeometry(boxGeo),
        new THREE.LineBasicMaterial({ color: 0x64748b, transparent: true, opacity: 0.55 })
      );
      slotWire.position.set(posX, posY, posZ);
      palletGroup.add(slotWire);

      const box = new THREE.Mesh(boxGeo, boxPlacedMat);
      box.position.set(posX, posY, posZ);
      box.castShadow = true;
      box.visible = false;
      palletGroup.add(box);
      palletMeshes.push(box);
    }
    scene.add(palletGroup);
  }

  // --- PROCEDURAL NEUROMEKA INDY7 DIGITAL COPY ---
  function createIndy7Robot() {
    const indyWhite = new THREE.MeshStandardMaterial({
      color: 0xf8fafc,
      roughness: 0.28,
      metalness: 0.15
    });
    const indyTeal = new THREE.MeshStandardMaterial({
      color: 0x0f766e,
      roughness: 0.35,
      metalness: 0.65,
      emissive: 0x042f2e,
      emissiveIntensity: 0.2
    });
    const darkMetal = new THREE.MeshStandardMaterial({
      color: 0x1e293b,
      roughness: 0.4,
      metalness: 0.8
    });
    const chromeMetal = new THREE.MeshStandardMaterial({
      color: 0xe2e8f0,
      roughness: 0.15,
      metalness: 0.95
    });

    robotBase = new THREE.Group();
    robotBase.position.set(0, 0, 0);

    // Base Pedestal (Link 0: Z=0 to Z=281mm without overlap)
    const baseFlange = new THREE.Mesh(new THREE.CylinderGeometry(105, 112, 20, 36), darkMetal);
    baseFlange.rotation.x = Math.PI / 2;
    baseFlange.position.z = 10;
    baseFlange.castShadow = true;
    robotBase.add(baseFlange);

    const baseColumn = new THREE.Mesh(new THREE.CylinderGeometry(80, 88, 245, 36), indyWhite);
    baseColumn.rotation.x = Math.PI / 2;
    baseColumn.position.z = 142.5;
    baseColumn.castShadow = true;
    robotBase.add(baseColumn);

    const baseTealRing = new THREE.Mesh(new THREE.CylinderGeometry(84, 84, 8, 36), indyTeal);
    baseTealRing.rotation.x = Math.PI / 2;
    baseTealRing.position.z = 269;
    robotBase.add(baseTealRing);

    // Halo Status LED Ring (Z=277 to 281mm)
    const haloMat = new THREE.MeshStandardMaterial({
      color: 0x10b981,
      emissive: 0x10b981,
      emissiveIntensity: 0.9
    });
    baseLedHalo = new THREE.Mesh(new THREE.CylinderGeometry(85, 85, 8, 36), haloMat);
    baseLedHalo.rotation.x = Math.PI / 2;
    baseLedHalo.position.z = 277;
    robotBase.add(baseLedHalo);

    // 6 Craig MDH Joint Transformation Groups Hierarchy
    for (let i = 0; i < 6; i++) {
      const g = new THREE.Group();
      g.matrixAutoUpdate = false;
      jointGroups.push(g);
    }

    robotBase.add(jointGroups[0]);
    jointGroups[0].add(jointGroups[1]);
    jointGroups[1].add(jointGroups[2]);
    jointGroups[2].add(jointGroups[3]);
    jointGroups[3].add(jointGroups[4]);
    jointGroups[4].add(jointGroups[5]);

    // =========================================================================
    // LINK 1: Shoulder Turret (Frame 1 at Z=300mm)
    // =========================================================================
    const link1MeshGroup = new THREE.Group();

    // Turret base interface (sits cleanly on top of base pedestal from Z=-19 to Z=0)
    const turretBasePlate = new THREE.Mesh(new THREE.CylinderGeometry(80, 80, 18, 32), darkMetal);
    turretBasePlate.rotation.x = Math.PI / 2;
    turretBasePlate.position.z = -10;
    turretBasePlate.castShadow = true;
    link1MeshGroup.add(turretBasePlate);

    const turretBody = new THREE.Mesh(new THREE.CylinderGeometry(72, 78, 28, 32), indyWhite);
    turretBody.rotation.x = Math.PI / 2;
    turretBody.position.z = 4;
    turretBody.castShadow = true;
    link1MeshGroup.add(turretBody);

    // Joint 2 Shoulder Axle Hub: Rotates around horizontal Y-axis (height natively along Y)
    const shoulderPivot = new THREE.Mesh(new THREE.CylinderGeometry(52, 52, 120, 32), darkMetal);
    shoulderPivot.position.set(0, 0, 0);
    shoulderPivot.castShadow = true;
    link1MeshGroup.add(shoulderPivot);

    const shoulderCapL = new THREE.Mesh(new THREE.CylinderGeometry(55, 55, 8, 32), indyTeal);
    shoulderCapL.position.set(0, 60, 0);
    link1MeshGroup.add(shoulderCapL);

    const shoulderCapR = new THREE.Mesh(new THREE.CylinderGeometry(55, 55, 8, 32), indyTeal);
    shoulderCapR.position.set(0, -60, 0);
    link1MeshGroup.add(shoulderCapR);

    jointGroups[0].add(link1MeshGroup);

    // =========================================================================
    // LINK 2: Upper Arm (Frame 2, extends along +X by 450mm to (450, 0, 3.5))
    // =========================================================================
    const link2MeshGroup = new THREE.Group();

    // Shoulder collar wrapping Joint 2 pivot without penetration
    const shoulderCollar = new THREE.Mesh(new THREE.CylinderGeometry(54, 54, 70, 32), darkMetal);
    shoulderCollar.position.set(0, 0, 0);
    link2MeshGroup.add(shoulderCollar);

    // Main Upper Arm structural beam (X=40 to X=410, perfectly clearing both joint hubs)
    const arm2Beam = new THREE.Mesh(new THREE.BoxGeometry(370, 58, 72), indyWhite);
    arm2Beam.position.set(225, 0, 1.75);
    arm2Beam.castShadow = true;
    link2MeshGroup.add(arm2Beam);

    const arm2StripeL = new THREE.Mesh(new THREE.BoxGeometry(320, 4, 24), indyTeal);
    arm2StripeL.position.set(225, 30, 1.75);
    link2MeshGroup.add(arm2StripeL);

    const arm2StripeR = new THREE.Mesh(new THREE.BoxGeometry(320, 4, 24), indyTeal);
    arm2StripeR.position.set(225, -30, 1.75);
    link2MeshGroup.add(arm2StripeR);

    // Elbow Joint 3 Hub at (450, 0, 3.5): Rotates around Z-axis (cylinder along Z)
    const elbowPivot = new THREE.Mesh(new THREE.CylinderGeometry(48, 48, 100, 32), darkMetal);
    elbowPivot.rotation.x = Math.PI / 2;
    elbowPivot.position.set(450, 0, 3.5);
    elbowPivot.castShadow = true;
    link2MeshGroup.add(elbowPivot);

    const elbowCap = new THREE.Mesh(new THREE.CylinderGeometry(51, 51, 8, 32), indyTeal);
    elbowCap.rotation.x = Math.PI / 2;
    elbowCap.position.set(450, 0, 3.5 + 50);
    link2MeshGroup.add(elbowCap);

    jointGroups[1].add(link2MeshGroup);

    // =========================================================================
    // LINK 3: Forearm (Frame 3 at elbow, extends along -Y by 350mm to (0, -350, 0))
    // =========================================================================
    const link3MeshGroup = new THREE.Group();

    // Elbow attachment collar wrapping Joint 3 pivot
    const elbowCollar = new THREE.Mesh(new THREE.CylinderGeometry(50, 50, 40, 32), darkMetal);
    elbowCollar.rotation.x = Math.PI / 2;
    elbowCollar.position.set(0, 0, 0);
    link3MeshGroup.add(elbowCollar);

    // Forearm main tube (spans from Y=-38mm to Y=-312mm, length 274mm along -Y)
    const arm3Cyl = new THREE.Mesh(new THREE.CylinderGeometry(44, 40, 274, 32), indyWhite);
    arm3Cyl.position.set(0, -175, 0);
    arm3Cyl.castShadow = true;
    link3MeshGroup.add(arm3Cyl);

    const arm3TealRing = new THREE.Mesh(new THREE.CylinderGeometry(44.5, 44.5, 12, 32), indyTeal);
    arm3TealRing.position.set(0, -175, 0);
    link3MeshGroup.add(arm3TealRing);

    // Wrist 1 Roll Hub at Y=-350mm: Joint 4 rotates around Y-axis (cylinder along Y)
    const wrist1Housing = new THREE.Mesh(new THREE.CylinderGeometry(42, 42, 46, 32), darkMetal);
    wrist1Housing.position.set(0, -350, 0);
    wrist1Housing.castShadow = true;
    link3MeshGroup.add(wrist1Housing);

    jointGroups[2].add(link3MeshGroup);

    // =========================================================================
    // LINK 4: 183mm Offset Wrist (Frame 4, extends along -Y by 183mm to (0, -183, 0))
    // =========================================================================
    const link4MeshGroup = new THREE.Group();

    // Offset cross link tube (spans from Y=-24mm to Y=-159mm, length 135mm along -Y)
    const wristCrossCyl = new THREE.Mesh(new THREE.CylinderGeometry(36, 36, 135, 32), indyWhite);
    wristCrossCyl.position.set(0, -91.5, 0);
    wristCrossCyl.castShadow = true;
    link4MeshGroup.add(wristCrossCyl);

    const wristCrossRing = new THREE.Mesh(new THREE.CylinderGeometry(38, 38, 10, 32), indyTeal);
    wristCrossRing.position.set(0, -91.5, 0);
    link4MeshGroup.add(wristCrossRing);

    // Wrist 2 Pitch Hub at Y=-183mm: Joint 5 rotates around Y-axis (cylinder along Y)
    const wrist2Housing = new THREE.Mesh(new THREE.CylinderGeometry(38, 38, 42, 32), darkMetal);
    wrist2Housing.position.set(0, -183, 0);
    wrist2Housing.castShadow = true;
    link4MeshGroup.add(wrist2Housing);

    jointGroups[3].add(link4MeshGroup);

    // =========================================================================
    // LINK 5: Tool Mount Link (Frame 5, extends along -Y by 228mm to (0, -228, 0))
    // =========================================================================
    const link5MeshGroup = new THREE.Group();

    // Tool link cylinder (spans from Y=-22mm to Y=-215mm, length 193mm along -Y)
    const toolLinkCyl = new THREE.Mesh(new THREE.CylinderGeometry(34, 32, 193, 32), indyWhite);
    toolLinkCyl.position.set(0, -118.5, 0);
    toolLinkCyl.castShadow = true;
    link5MeshGroup.add(toolLinkCyl);

    const toolLinkTeal = new THREE.Mesh(new THREE.CylinderGeometry(34.5, 34.5, 10, 32), indyTeal);
    toolLinkTeal.position.set(0, -118.5, 0);
    link5MeshGroup.add(toolLinkTeal);

    const flangeMountCollar = new THREE.Mesh(new THREE.CylinderGeometry(33, 33, 8, 32), darkMetal);
    flangeMountCollar.position.set(0, -224, 0);
    link5MeshGroup.add(flangeMountCollar);

    jointGroups[4].add(link5MeshGroup);

    // =========================================================================
    // LINK 6: Tool Flange & Parallel Pneumatic Gripper (Frame 6 at Flange Face)
    // =========================================================================
    const link6MeshGroup = new THREE.Group();

    // Tool Flange Disc at Z=5mm (along Tool Z-axis)
    const flangeMesh = new THREE.Mesh(new THREE.CylinderGeometry(32, 32, 10, 32), darkMetal);
    flangeMesh.rotation.x = Math.PI / 2;
    flangeMesh.position.z = 5;
    link6MeshGroup.add(flangeMesh);

    // Streamlined Gripper Body (Box 46 x 58 x 32)
    const gripBody = new THREE.Mesh(new THREE.BoxGeometry(46, 58, 32), indyTeal);
    gripBody.position.z = 26;
    gripBody.castShadow = true;
    link6MeshGroup.add(gripBody);

    const pCyl = new THREE.Mesh(new THREE.CylinderGeometry(11, 11, 26, 16), darkMetal);
    pCyl.rotation.x = Math.PI / 2;
    pCyl.position.set(0, 0, 26);
    link6MeshGroup.add(pCyl);

    // Pneumatic guide rods
    const rodL = new THREE.Mesh(new THREE.CylinderGeometry(3, 3, 24, 12), chromeMetal);
    rodL.rotation.x = Math.PI / 2;
    rodL.position.set(-15, 0, 38);
    link6MeshGroup.add(rodL);

    const rodR = new THREE.Mesh(new THREE.CylinderGeometry(3, 3, 24, 12), chromeMetal);
    rodR.rotation.x = Math.PI / 2;
    rodR.position.set(15, 0, 38);
    link6MeshGroup.add(rodR);

    // Gripper Jaws
    const jawMat = new THREE.MeshStandardMaterial({ color: 0x94a3b8, roughness: 0.2, metalness: 0.9 });
    jawLeft = new THREE.Mesh(new THREE.BoxGeometry(8, 12, 32), jawMat);
    jawLeft.position.set(-15, 0, 54);
    jawLeft.castShadow = true;
    link6MeshGroup.add(jawLeft);

    jawRight = new THREE.Mesh(new THREE.BoxGeometry(8, 12, 32), jawMat);
    jawRight.position.set(15, 0, 54);
    jawRight.castShadow = true;
    link6MeshGroup.add(jawRight);

    // Held workpiece
    const heldMat = new THREE.MeshStandardMaterial({
      color: 0x06b6d4,
      roughness: 0.3,
      metalness: 0.5,
      emissive: 0x0891b2,
      emissiveIntensity: 0.4
    });
    heldWorkpieceMesh = new THREE.Mesh(new THREE.CylinderGeometry(18, 18, 24, 24), heldMat);
    heldWorkpieceMesh.rotation.x = Math.PI / 2;
    heldWorkpieceMesh.position.set(0, 0, 54);
    heldWorkpieceMesh.visible = false;
    link6MeshGroup.add(heldWorkpieceMesh);

    jointGroups[5].add(link6MeshGroup);
    scene.add(robotBase);

    updateRobotKinematics(currentQ);
  }

  // --- EXACT CRAIG MDH KINEMATICS ENGINE ---
  function computeMDHMatrix(targetMatrix, alphaDeg, a, thetaDeg, d) {
    const alpha = THREE.MathUtils.degToRad(alphaDeg);
    const theta = THREE.MathUtils.degToRad(thetaDeg);
    const ca = Math.cos(alpha), sa = Math.sin(alpha);
    const ct = Math.cos(theta), st = Math.sin(theta);

    targetMatrix.set(
      ct,        -st,        0,     a,
      ca * st,    ca * ct,  -sa,  -sa * d,
      sa * st,    sa * ct,   ca,   ca * d,
      0,          0,         0,     1
    );
  }

  function updateRobotKinematics(qDegs) {
    for (let i = 0; i < 6; i++) {
      const p = MDH_PARAMS[i];
      computeMDHMatrix(
        jointGroups[i].matrix,
        p.alpha,
        p.a,
        p.theta0 + qDegs[i],
        p.d0
      );
      jointGroups[i].matrixWorldNeedsUpdate = true;
    }
  }

  // --- 3D WAYPOINT MARKERS IN WORKCELL SCENE ---
  function update3DWaypointMarkers(waypoints) {
    if (!waypointMarkersGroup) return;
    
    // Clear old markers
    while (waypointMarkersGroup.children.length > 0) {
      const obj = waypointMarkersGroup.children[0];
      waypointMarkersGroup.remove(obj);
    }

    const sphereGeo = new THREE.SphereGeometry(12, 16, 16);
    const sphereMat = new THREE.MeshStandardMaterial({
      color: 0x06b6d4,
      emissive: 0x0891b2,
      emissiveIntensity: 0.6,
      roughness: 0.3,
      metalness: 0.5
    });

    waypoints.forEach((wp, index) => {
      if (!wp.p || wp.p.length < 3) return;

      const marker = new THREE.Group();
      marker.position.set(wp.p[0], wp.p[1], wp.p[2]);

      const sphere = new THREE.Mesh(sphereGeo, sphereMat);
      sphere.castShadow = true;
      marker.add(sphere);

      // Mini RGB Triad
      const axes = new THREE.AxesHelper(35);
      marker.add(axes);

      // Label sprite
      const canvas = document.createElement("canvas");
      canvas.width = 256;
      canvas.height = 64;
      const ctx = canvas.getContext("2d");
      ctx.fillStyle = "rgba(15, 23, 42, 0.85)";
      ctx.roundRect(0, 0, 256, 64, 8);
      ctx.fill();
      ctx.strokeStyle = "#06b6d4";
      ctx.lineWidth = 3;
      ctx.roundRect(0, 0, 256, 64, 8);
      ctx.stroke();
      ctx.fillStyle = "#ffffff";
      ctx.font = "bold 22px 'JetBrains Mono', monospace";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(`#${index + 1}: ${wp.name.substring(0, 14)}`, 128, 32);

      const tex = new THREE.CanvasTexture(canvas);
      const spriteMat = new THREE.SpriteMaterial({ map: tex, depthTest: false });
      const sprite = new THREE.Sprite(spriteMat);
      sprite.position.set(0, 0, 30);
      sprite.scale.set(70, 18, 1);
      marker.add(sprite);

      waypointMarkersGroup.add(marker);
    });
  }

  // --- TRAJECTORY RIBBON (TCP TRAIL) ---
  function createTrajectoryRibbon() {
    const maxPoints = MAX_TRAIL_POINTS;
    const positions = new Float32Array(maxPoints * 3);
    trailGeometry = new THREE.BufferGeometry();
    trailGeometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));

    const trailMaterial = new THREE.LineBasicMaterial({
      color: 0x06b6d4,
      linewidth: 3,
      transparent: true,
      opacity: 0.75
    });

    trailLine = new THREE.Line(trailGeometry, trailMaterial);
    scene.add(trailLine);
  }

  function updateTrajectoryRibbon(tipPos) {
    trailPoints.push(tipPos.clone());
    if (trailPoints.length > MAX_TRAIL_POINTS) {
      trailPoints.shift();
    }

    const positions = trailGeometry.attributes.position.array;
    for (let i = 0; i < trailPoints.length; i++) {
      positions[i * 3]     = trailPoints[i].x;
      positions[i * 3 + 1] = trailPoints[i].y;
      positions[i * 3 + 2] = trailPoints[i].z;
    }
    trailGeometry.setDrawRange(0, trailPoints.length);
    trailGeometry.attributes.position.needsUpdate = true;
  }

  // --- WEBSOCKET TELEMETRY CLIENT ---
  let ws = null;
  function initWebSocket() {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const host = window.location.host;
    const url = `${proto}//${host}/ws/telemetry`;

    ws = new WebSocket(url);
    ws.onopen = () => console.log("[WS] Telemetry connected.");
    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        handleTelemetry(data);
      } catch (e) {}
    };
    ws.onclose = () => {
      handleOfflineState();
      setTimeout(initWebSocket, 2000);
    };
  }

  function handleTelemetry(data) {
    lastServerState = data;
    currentMode = data.mode;
    isHardwareConnected = data.hardware_connected;
    directTeachingState = data.direct_teaching || false;

    if (data.q && data.q.length === 6) {
      targetQ = data.q;
    }
    if (data.p && data.p.length >= 6) {
      currentP = data.p;
    }
    gripperState = data.gripper_closed;
    heldWorkpiece = data.held_workpiece;

    // Check waypoints list update
    if (data.waypoints && JSON.stringify(data.waypoints) !== JSON.stringify(currentWaypointsList)) {
      currentWaypointsList = data.waypoints;
      renderWaypointsTable(currentWaypointsList);
      update3DWaypointMarkers(currentWaypointsList);
    }

    updateHUD(data);
  }

  function handleOfflineState() {
    isHardwareConnected = false;
    currentMode = "DISCONNECTED";
    const fakeOfflineData = {
      mode: "DISCONNECTED",
      hardware_connected: false,
      status_msg: "NO CONNECTION to robot arm (192.168.3.7)",
      op_state: 0,
      op_state_name: "NO CONNECTION",
      is_moving: false,
      plc_io: { pb1: false, pb2: false, stop: false, mag_sensor: false, do0_open: false, do1_close: false },
      q: currentQ,
      p: [350.0, -186.5, 522.0, 0, -180, 0],
      pallet_count: 0,
      max_items: 8,
      magazine_count: 8,
      gripper_closed: false,
      held_workpiece: false,
      tact_time: 0,
      telemetry_hz: 0,
      waypoints: currentWaypointsList
    };
    updateHUD(fakeOfflineData);
  }

  function sendCmd(cmd, payload = {}) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ cmd, ...payload }));
    } else {
      fetch(`/api/${cmd}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
    }
  }

  function applyLightingMode(index) {
    currentLightingIndex = index % LIGHTING_MODES.length;
    const cfg = LIGHTING_MODES[currentLightingIndex];
    if (ambientLight) ambientLight.intensity = cfg.amb;
    if (hemiLight)    hemiLight.intensity = cfg.hemi;
    if (dirLight)     dirLight.intensity = cfg.dir;
    if (studioLight)  studioLight.intensity = cfg.studio;
    if (renderer)     renderer.toneMappingExposure = cfg.expo;
    if (scene && scene.fog) scene.fog.density = cfg.fog;

    const btn = document.getElementById("btnLighting");
    if (btn) {
      btn.textContent = cfg.label;
      btn.style.borderColor = cfg.name === "ULTRA" ? "#facc15" : cfg.name === "BRIGHT" ? "var(--accent-amber)" : "var(--text-dim)";
      btn.style.color = cfg.name === "ULTRA" ? "#facc15" : cfg.name === "BRIGHT" ? "var(--accent-amber)" : "var(--text-dim)";
    }
  }

  // --- MULTI-PURPOSE CENTER UI INITIALIZATION ---
  function initMultiCenter() {
    // Keep engineering telemetry available without crowding the operator view.
    const diagnosticsSource = document.querySelector(".diagnostics-source");
    const diagnosticsMount = document.getElementById("diagnosticsMount");
    if (diagnosticsSource && diagnosticsMount) {
      while (diagnosticsSource.firstChild) {
        diagnosticsMount.appendChild(diagnosticsSource.firstChild);
      }
      diagnosticsSource.remove();
    }

    // 1. Drawer Tabs Switching
    const tabs = document.querySelectorAll(".drawer-tab");
    tabs.forEach(tab => {
      tab.addEventListener("click", () => {
        tabs.forEach(t => t.classList.remove("active"));
        document.querySelectorAll(".tab-content").forEach(c => c.classList.remove("active"));

        tab.classList.add("active");
        const targetId = tab.dataset.tab;
        const content = document.getElementById(targetId);
        if (content) content.classList.add("active");

        // Ensure drawer is expanded when switching tabs
        document.getElementById("multiCenterDrawer").classList.remove("collapsed");
      });
    });

    // 2. Drawer Minimize / Expand
    const drawer = document.getElementById("multiCenterDrawer");
    const btnCollapse = document.getElementById("btnDrawerCollapse");
    const btnToggleDrawer = document.getElementById("btnDrawerToggle");

    if (btnCollapse) {
      btnCollapse.addEventListener("click", () => {
        drawer.classList.toggle("collapsed");
      });
    }

    if (btnToggleDrawer) {
      btnToggleDrawer.addEventListener("click", () => {
        drawer.classList.toggle("collapsed");
      });
    }

    // 3. Tab 1: Action Programs Buttons
    document.getElementById("btnProgPalletize").addEventListener("click", () => sendCmd("pb1"));
    document.getElementById("btnProgPutBack").addEventListener("click", () => sendCmd("pb2"));
    document.getElementById("btnProgHome").addEventListener("click", () => sendCmd("home"));
    document.getElementById("btnProgZero").addEventListener("click", () => sendCmd("zero"));
    document.getElementById("btnProgRecover").addEventListener("click", () => sendCmd("recover"));
    document.getElementById("btnProgResetPallet").addEventListener("click", () => fetch("/api/reset_pallet", { method: "POST" }));
    document.getElementById("btnProgStop").addEventListener("click", () => sendCmd("stop", { active: true }));
    const btnHeaderStop = document.getElementById("btnHeaderStop");
    if (btnHeaderStop) {
      btnHeaderStop.addEventListener("click", () => {
        const stopIsActive = btnHeaderStop.dataset.active === "true";
        sendCmd("stop", { active: !stopIsActive });
      });
    }

    // Direct Teaching Free-Drive Toggle
    const btnDirectTeach = document.getElementById("btnToggleDirectTeach");
    if (btnDirectTeach) {
      btnDirectTeach.addEventListener("click", () => {
        const nextState = !directTeachingState;
        sendCmd("direct_teaching", { enable: nextState });
      });
    }

    // 4. Tab 2: Web Teach Pendant Controls
    const btnJogModeJoint = document.getElementById("btnJogModeJoint");
    const btnJogModeTask = document.getElementById("btnJogModeTask");
    const jointJogPad = document.getElementById("jointJogPad");
    const taskJogPad = document.getElementById("taskJogPad");
    const jointStepGroup = document.getElementById("jointStepGroup");
    const taskStepGroup = document.getElementById("taskStepGroup");

    btnJogModeJoint.addEventListener("click", () => {
      activeJogMode = "joint";
      btnJogModeJoint.classList.add("active");
      btnJogModeTask.classList.remove("active");
      jointJogPad.style.display = "grid";
      taskJogPad.style.display = "none";
      jointStepGroup.style.display = "flex";
      taskStepGroup.style.display = "none";
    });

    btnJogModeTask.addEventListener("click", () => {
      activeJogMode = "task";
      btnJogModeTask.classList.add("active");
      btnJogModeJoint.classList.remove("active");
      taskJogPad.style.display = "grid";
      jointJogPad.style.display = "none";
      taskStepGroup.style.display = "flex";
      jointStepGroup.style.display = "none";
    });

    // Step Presets
    jointStepGroup.querySelectorAll(".btn-step").forEach(btn => {
      btn.addEventListener("click", () => {
        jointStepGroup.querySelectorAll(".btn-step").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        currentJointStep = parseFloat(btn.dataset.step);
      });
    });

    taskStepGroup.querySelectorAll(".btn-step").forEach(btn => {
      btn.addEventListener("click", () => {
        taskStepGroup.querySelectorAll(".btn-step").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        currentTaskStep = parseFloat(btn.dataset.step);
      });
    });

    // Speed Slider
    const speedSlider = document.getElementById("pendantSpeedSlider");
    const speedVal = document.getElementById("pendantSpeedVal");
    if (speedSlider) {
      speedSlider.addEventListener("input", (e) => {
        currentSpeedRatio = parseInt(e.target.value);
        if (speedVal) speedVal.textContent = `${currentSpeedRatio}%`;
        fetch("/api/robot/speed", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ speed_ratio: currentSpeedRatio })
        });
      });
    }

    // Gripper Buttons
    document.getElementById("btnPendantGripperOpen").addEventListener("click", () => sendCmd("gripper", { close: false }));
    document.getElementById("btnPendantGripperClose").addEventListener("click", () => sendCmd("gripper", { close: true }));
    document.getElementById("btnPendantStopJog").addEventListener("click", () => sendCmd("jog_stop"));

    // Jog Buttons (+/- Action)
    document.querySelectorAll(".btn-jog").forEach(btn => {
      const executeJog = () => {
        const type = btn.dataset.type;
        const dir = parseInt(btn.dataset.dir);
        if (type === "joint") {
          const idx = parseInt(btn.dataset.idx);
          sendCmd("jog_joint", {
            joint_idx: idx,
            step_deg: dir * currentJointStep,
            vel_ratio: currentSpeedRatio
          });
        } else if (type === "task") {
          const axis = btn.dataset.axis;
          sendCmd("jog_task", {
            axis: axis,
            step_val: dir * currentTaskStep,
            vel_ratio: currentSpeedRatio
          });
        }
      };

      btn.addEventListener("click", executeJog);

      // Support continuous hold jog
      btn.addEventListener("mousedown", () => {
        if (continuousJogInterval) clearInterval(continuousJogInterval);
        continuousJogInterval = setInterval(executeJog, 140);
      });

      const clearHold = () => {
        if (continuousJogInterval) {
          clearInterval(continuousJogInterval);
          continuousJogInterval = null;
          sendCmd("jog_stop");
        }
      };
      btn.addEventListener("mouseup", clearHold);
      btn.addEventListener("mouseleave", clearHold);
    });

    // 5. Tab 3: Waypoint Management & Mission Planner
    initWaypointManager();
  }

  // --- WAYPOINT MANAGER & MISSION PLANNER ---
  function initWaypointManager() {
    const modal = document.getElementById("modalAcquireWp");
    const btnOpenAcquire = document.getElementById("btnAcquireCurrentPose");
    const btnCancel = document.getElementById("btnAcquireCancel");
    const btnCancel2 = document.getElementById("btnAcquireCancel2");
    const btnConfirm = document.getElementById("btnAcquireConfirm");

    if (btnOpenAcquire) {
      btnOpenAcquire.addEventListener("click", () => {
        // Pre-fill modal
        document.getElementById("wpInputName").value = `WP_${currentWaypointsList.length + 1}`;
        document.getElementById("wpAcquirePreviewJoints").textContent = `Q: [${currentQ.map(x => x.toFixed(1)).join(", ")}]`;
        document.getElementById("wpAcquirePreviewPose").textContent = `P: [${currentP.map(x => x.toFixed(1)).join(", ")}]`;
        modal.style.display = "flex";
      });
    }

    const closeModal = () => { modal.style.display = "none"; };
    if (btnCancel) btnCancel.addEventListener("click", closeModal);
    if (btnCancel2) btnCancel2.addEventListener("click", closeModal);

    if (btnConfirm) {
      btnConfirm.addEventListener("click", () => {
        const name = document.getElementById("wpInputName").value.trim() || `WP_${currentWaypointsList.length + 1}`;
        const move_type = document.getElementById("wpInputType").value;
        const gripper = document.getElementById("wpInputGripper").value;
        const speed = parseInt(document.getElementById("wpInputSpeed").value) || 25;
        const dwell = parseFloat(document.getElementById("wpInputDwell").value) || 0.5;

        fetch("/api/waypoints/acquire", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name, move_type, gripper, speed, dwell })
        }).then(res => res.json()).then(data => {
          if (data.success) {
            closeModal();
            fetchWaypoints();
          }
        });
      });
    }

    // Sequence Controls
    const btnRunSeq = document.getElementById("btnRunSequence");
    const btnStopSeq = document.getElementById("btnStopSequence");
    const repeatSelect = document.getElementById("seqRepeatCount");

    if (btnRunSeq) {
      btnRunSeq.addEventListener("click", () => {
        const repeat = parseInt(repeatSelect.value) || 1;
        sendCmd("start_sequence", { repeat_count: repeat });
      });
    }

    if (btnStopSeq) {
      btnStopSeq.addEventListener("click", () => {
        sendCmd("stop_sequence");
      });
    }

    // Persistence Buttons
    document.getElementById("btnSaveWpToDisk").addEventListener("click", () => {
      fetch("/api/waypoints", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ waypoints: currentWaypointsList })
      }).then(() => alert("Waypoints saved to disk (src/digitaltwin/waypoints.json)!"));
    });

    document.getElementById("btnReloadWp").addEventListener("click", fetchWaypoints);

    // Initial load
    fetchWaypoints();
  }

  function fetchWaypoints() {
    fetch("/api/waypoints")
      .then(res => res.json())
      .then(list => {
        currentWaypointsList = list;
        renderWaypointsTable(list);
        update3DWaypointMarkers(list);
      });
  }

  function renderWaypointsTable(waypoints) {
    const tbody = document.getElementById("wpTableBody");
    if (!tbody) return;
    tbody.innerHTML = "";

    if (!waypoints || waypoints.length === 0) {
      tbody.innerHTML = `<tr><td colspan="9" style="text-align: center; color: var(--text-dim); padding: 20px;">No waypoints acquired. Click "+ ACQUIRE CURRENT WAYPOINT" to save poses.</td></tr>`;
      return;
    }

    waypoints.forEach((wp, idx) => {
      const tr = document.createElement("tr");

      const pStr = wp.p ? `[${wp.p.slice(0, 3).map(x => x.toFixed(1)).join(", ")}]` : "--";
      const qStr = wp.q ? `[${wp.q.map(x => x.toFixed(0)).join(", ")}]` : "--";
      const gripBadge = wp.gripper === "open" ? "<span style='color: var(--accent-cyan);'>OPEN</span>" : wp.gripper === "close" ? "<span style='color: var(--accent-emerald);'>CLOSE</span>" : "<span style='color: var(--text-dim);'>KEEP</span>";
      const typeBadge = `<span class="wp-badge-type ${wp.move_type === 'MoveL' ? 'type-movel' : 'type-movej'}">${wp.move_type || 'MoveJ'}</span>`;

      tr.innerHTML = `
        <td style="color: var(--text-dim); font-weight: 700;">${idx + 1}</td>
        <td style="font-weight: 700; color: #fff;">${wp.name}</td>
        <td>${typeBadge}</td>
        <td style="color: var(--accent-cyan);">${pStr}</td>
        <td style="color: var(--text-muted); font-size: 10px;">${qStr}</td>
        <td>${wp.speed || 25}%</td>
        <td>${gripBadge}</td>
        <td>${wp.dwell || 0}s</td>
        <td>
          <div class="wp-action-btns">
            <button class="btn-wp-goto" data-id="${wp.id}">MOVE TO</button>
            <button class="btn-wp-del" data-id="${wp.id}">&times;</button>
          </div>
        </td>
      `;

      tr.querySelector(".btn-wp-goto").addEventListener("click", () => {
        sendCmd("goto_wp", { id: wp.id });
      });

      tr.querySelector(".btn-wp-del").addEventListener("click", () => {
        fetch("/api/waypoints/delete", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: wp.id })
        }).then(fetchWaypoints);
      });

      tbody.appendChild(tr);
    });
  }

  // --- HUD & UI INTERACTION ---
  function initUI() {
    const btnLighting = document.getElementById("btnLighting");
    if (btnLighting) {
      btnLighting.addEventListener("click", () => applyLightingMode(currentLightingIndex + 1));
    }

    const btnReconnect = document.getElementById("btnReconnect");
    if (btnReconnect) {
      btnReconnect.addEventListener("click", () => {
        fetch("/api/reconnect", { method: "POST" });
        sendCmd("reconnect");
      });
    }

    const btnOverlayReconnect = document.getElementById("btnOverlayReconnect");
    if (btnOverlayReconnect) {
      btnOverlayReconnect.addEventListener("click", () => {
        fetch("/api/reconnect", { method: "POST" });
        sendCmd("reconnect");
      });
    }

    const btnOverlaySim = document.getElementById("btnOverlaySim");
    if (btnOverlaySim) {
      btnOverlaySim.addEventListener("click", () => {
        fetch("/api/simulation", { method: "POST" });
        sendCmd("simulation");
      });
    }

    const btnMode = document.getElementById("btnToggleMode");
    if (btnMode) {
      btnMode.addEventListener("click", () => {
        if (currentMode === "HARDWARE_LIVE") {
          fetch("/api/simulation", { method: "POST" });
        } else {
          fetch("/api/reconnect", { method: "POST" });
        }
      });
    }

    // Quick Jaw toggle & Trail clear
    const btnQuickGripper = document.getElementById("btnQuickGripper");
    if (btnQuickGripper) {
      btnQuickGripper.addEventListener("click", () => {
        sendCmd("gripper", { close: !gripperState });
      });
    }

    const btnClearTrail = document.getElementById("btnClearTrail");
    if (btnClearTrail) {
      btnClearTrail.addEventListener("click", () => {
        trailPoints = [];
        trailGeometry.setDrawRange(0, 0);
        trailGeometry.attributes.position.needsUpdate = true;
      });
    }

    // Camera Presets
    document.getElementById("camIso").addEventListener("click", () => {
      setCameraView([1100, -1300, 1100], [160, 320, 260], "camIso");
    });
    document.getElementById("camTop").addEventListener("click", () => {
      setCameraView([160, 350, 2200], [160, 350, 0], "camTop");
    });
    document.getElementById("camFeeder").addEventListener("click", () => {
      setCameraView([550, 850, 520], [WORKCELL.pickPos[0], WORKCELL.pickPos[1], WORKCELL.pickPos[2] + 40], "camFeeder");
    });
    document.getElementById("camPallet").addEventListener("click", () => {
      setCameraView([420, 480, 600], [WORKCELL.dropBase[0], WORKCELL.dropBase[1], WORKCELL.dropBase[2] + 40], "camPallet");
    });
  }

  function setCameraView(pos, look, activeId) {
    camTargetPos = new THREE.Vector3(...pos);
    camTargetLook = new THREE.Vector3(...look);

    document.querySelectorAll(".btn-cam").forEach(b => b.classList.remove("active"));
    const btn = document.getElementById(activeId);
    if (btn) btn.classList.add("active");
  }

  function updateHUD(data) {
    const isConn = data.hardware_connected === true;
    const isSim = data.mode === "SIMULATION";

    // 1. Connection Status Badge & Reconnect Button
    const connBadge = document.getElementById("connStatusBadge");
    const connText = document.getElementById("connStatusText");
    const btnReconnect = document.getElementById("btnReconnect");
    const hzBadge = document.getElementById("telemetryHzBadge");
    const offlineOverlay = document.getElementById("offlineOverlay");

    if (isConn) {
      if (connBadge) {
        connBadge.className = "badge conn-connected";
        connText.textContent = `LIVE: ${data.robot_ip || "192.168.3.7"}`;
      }
      if (btnReconnect) btnReconnect.style.display = "none";
      if (hzBadge) {
        hzBadge.style.display = "inline-block";
        hzBadge.textContent = `${data.telemetry_hz || 30} Hz`;
      }
      if (offlineOverlay) offlineOverlay.style.display = "none";

      if (baseLedHalo) {
        baseLedHalo.material.color.setHex(0x10b981);
        baseLedHalo.material.emissive.setHex(0x10b981);
      }
    } else if (isSim) {
      if (connBadge) {
        connBadge.className = "badge conn-sim";
        connText.textContent = "3D SIMULATION";
      }
      if (btnReconnect) btnReconnect.style.display = "inline-block";
      if (hzBadge) hzBadge.style.display = "none";
      if (offlineOverlay) offlineOverlay.style.display = "none";

      if (baseLedHalo) {
        baseLedHalo.material.color.setHex(0x06b6d4);
        baseLedHalo.material.emissive.setHex(0x06b6d4);
      }
    } else {
      if (connBadge) {
        connBadge.className = "badge conn-disconnected";
        connText.textContent = "NO CONNECTION";
      }
      if (btnReconnect) btnReconnect.style.display = "inline-block";
      if (hzBadge) hzBadge.style.display = "none";
      if (offlineOverlay) offlineOverlay.style.display = "flex";

      if (baseLedHalo) {
        baseLedHalo.material.color.setHex(0xef4444);
        baseLedHalo.material.emissive.setHex(0xef4444);
      }
    }

    // 2. OpState Badge
    const badge = document.getElementById("opBadge");
    const opText = document.getElementById("opText");
    badge.className = "badge";
    if (!isConn && !isSim) {
      badge.classList.add("badge-stop");
      opText.textContent = "OFFLINE (NO TELEMETRY)";
    } else if (data.plc_io && data.plc_io.stop) {
      badge.classList.add("badge-stop");
      opText.textContent = "STOP ACTIVE";
    } else if (data.is_moving) {
      badge.classList.add("badge-moving");
      opText.textContent = data.op_state_name || "OP_MOVING (6)";
    } else {
      badge.classList.add("badge-idle");
      opText.textContent = data.op_state_name || "OP_IDLE (5)";
    }

    // 3. Mode Toggle Button
    const btnMode = document.getElementById("btnToggleMode");
    if (btnMode) {
      if (isConn) {
        btnMode.textContent = "MODE: HARDWARE LIVE";
        btnMode.style.borderColor = "var(--accent-emerald)";
        btnMode.style.color = "var(--accent-emerald)";
      } else if (isSim) {
        btnMode.textContent = "MODE: 3D SIMULATION";
        btnMode.style.borderColor = "var(--accent-cyan)";
        btnMode.style.color = "var(--accent-cyan)";
      } else {
        btnMode.textContent = "MODE: DISCONNECTED";
        btnMode.style.borderColor = "var(--accent-crimson)";
        btnMode.style.color = "var(--accent-crimson)";
      }
    }

    const btnHeaderStop = document.getElementById("btnHeaderStop");
    if (btnHeaderStop) {
      const stopIsActive = !!(data.plc_io && data.plc_io.stop);
      btnHeaderStop.dataset.active = stopIsActive ? "true" : "false";
      btnHeaderStop.innerHTML = stopIsActive
        ? "CLEAR STOP <span>RESET REQUEST</span>"
        : "STOP MOTION <span>CAT 2</span>";
      btnHeaderStop.classList.toggle("is-active", stopIsActive);
    }

    // Direct Teaching Button state
    const btnDirectTeach = document.getElementById("btnToggleDirectTeach");
    if (btnDirectTeach) {
      if (data.direct_teaching) {
        btnDirectTeach.style.background = "#f59e0b";
        btnDirectTeach.style.color = "#000";
        btnDirectTeach.textContent = "👐 DIRECT TEACH: ON";
      } else {
        btnDirectTeach.style.background = "rgba(255, 255, 255, 0.08)";
        btnDirectTeach.style.color = "var(--text-main)";
        btnDirectTeach.textContent = "👐 DIRECT TEACH: OFF";
      }
    }

    // 4. Joint Gauges
    for (let i = 0; i < 6; i++) {
      const qVal = data.q[i] || 0;
      const jValEl = document.getElementById(`jVal${i}`);
      const jBarEl = document.getElementById(`jBar${i}`);
      if (jValEl) jValEl.textContent = `${qVal.toFixed(1)}°`;
      if (jBarEl) {
        const pct = Math.max(0, Math.min(100, ((qVal + 175) / 350) * 100));
        jBarEl.style.width = `${pct}%`;
      }

      // Update Teach Pendant Joint Angle labels
      const jogVal = document.getElementById(`jogValJ${i+1}`);
      if (jogVal) jogVal.textContent = `${qVal.toFixed(1)}°`;
    }

    // 5. Pose values
    if (data.p && data.p.length >= 6) {
      document.getElementById("poseX").textContent = data.p[0].toFixed(1);
      document.getElementById("poseY").textContent = data.p[1].toFixed(1);
      document.getElementById("poseZ").textContent = data.p[2].toFixed(1);
      document.getElementById("poseU").textContent = `${data.p[3].toFixed(1)}°`;
      document.getElementById("poseV").textContent = `${data.p[4].toFixed(1)}°`;
      document.getElementById("poseW").textContent = `${data.p[5].toFixed(1)}°`;

      // Update Teach Pendant Task labels
      const jogX = document.getElementById("jogValX");
      const jogY = document.getElementById("jogValY");
      const jogZ = document.getElementById("jogValZ");
      const jogU = document.getElementById("jogValU");
      const jogV = document.getElementById("jogValV");
      const jogW = document.getElementById("jogValW");
      if (jogX) jogX.textContent = `${data.p[0].toFixed(1)} mm`;
      if (jogY) jogY.textContent = `${data.p[1].toFixed(1)} mm`;
      if (jogZ) jogZ.textContent = `${data.p[2].toFixed(1)} mm`;
      if (jogU) jogU.textContent = `${data.p[3].toFixed(1)}°`;
      if (jogV) jogV.textContent = `${data.p[4].toFixed(1)}°`;
      if (jogW) jogW.textContent = `${data.p[5].toFixed(1)}°`;
    }

    // 6. Gripper Status
    const gripText = document.getElementById("gripperStatusText");
    if (data.gripper_closed) {
      gripText.textContent = data.held_workpiece ? "CLOSED (DO1) - GRIPPED" : "CLOSED (DO1)";
      gripText.style.color = "#10b981";
    } else {
      gripText.textContent = "OPEN (DO0)";
      gripText.style.color = "#06b6d4";
    }

    if (jawLeft && jawRight) {
      const jawOffset = data.gripper_closed ? 9 : 15;
      jawLeft.position.x = -jawOffset;
      jawRight.position.x = jawOffset;
    }
    if (heldWorkpieceMesh) {
      heldWorkpieceMesh.visible = !!data.held_workpiece;
    }

    // 7. I/O Signals
    const ioStop = document.getElementById("ioLedStop");
    const ioPb1 = document.getElementById("ioLedPb1");
    const ioPb2 = document.getElementById("ioLedPb2");
    const ioSensor = document.getElementById("ioLedSensor");
    const ioDo0 = document.getElementById("ioLedDo0");
    const ioDo1 = document.getElementById("ioLedDo1");

    if (data.plc_io) {
      if (ioStop) ioStop.className = `io-pill ${data.plc_io.stop ? "active-red" : ""}`;
      if (ioPb1) ioPb1.className = `io-pill ${data.plc_io.pb1 ? "active-green" : ""}`;
      if (ioPb2) ioPb2.className = `io-pill ${data.plc_io.pb2 ? "active-blue" : ""}`;
      if (ioSensor) ioSensor.className = `io-pill ${data.plc_io.mag_sensor ? "active-green" : ""}`;
      if (ioDo0) ioDo0.className = `io-pill ${data.plc_io.do0_open ? "active-cyan" : ""}`;
      if (ioDo1) ioDo1.className = `io-pill ${data.plc_io.do1_close ? "active-green" : ""}`;
    }

    // 8. Pallet Matrix Grid
    document.getElementById("palletCountTag").textContent = `${data.pallet_count} / ${data.max_items} PLACED`;
    for (let i = 0; i < 8; i++) {
      const cell = document.getElementById(`slotCell${i}`);
      const isPlaced = data.slots && data.slots[i] && data.slots[i].placed;
      if (cell) {
        cell.className = "slot-cell";
        if (isPlaced) cell.classList.add("occupied");
      }
      if (palletMeshes[i]) {
        palletMeshes[i].visible = isPlaced;
      }
    }

    // 9. Feeder Count
    document.getElementById("feederCountText").textContent = `${data.magazine_count} / 8`;
    for (let i = 0; i < 8; i++) {
      if (feederStack[i]) {
        feederStack[i].visible = i < data.magazine_count;
      }
    }

    // 10. Tact & Sequence Status Ticker
    document.getElementById("tactTimeText").textContent = `${data.tact_time ? data.tact_time.toFixed(2) : "0.00"}s`;
    document.getElementById("sequenceStatusTicker").textContent = data.status_msg || "Idle";

    // 11. Motion Phase & Approach/Extract Angle Information
    if (data.motion_phase) {
      const phaseTag = document.getElementById("motionPhaseTag");
      const angleTag = document.getElementById("motionAngleTag");
      const phaseDesc = document.getElementById("motionPhaseDesc");

      const phase = data.motion_phase.phase || "IDLE";
      const angle = data.motion_phase.angle || {};

      if (phaseTag) {
        phaseTag.textContent = `PHASE: ${phase}`;
        phaseTag.className = `motion-phase-tag phase-${phase.toLowerCase()}`;
      }

      if (angleTag && (angle.u !== undefined)) {
        angleTag.textContent = `U: ${angle.u.toFixed(2)}° | V: ${angle.v.toFixed(2)}° | W: ${angle.w.toFixed(2)}°`;
      }

      if (phaseDesc) {
        phaseDesc.textContent = angle.desc || "Collinear Approach & Extract Angle Tracking";
      }
    }

    const progTag = document.getElementById("activeProgTag");
    if (progTag && data.sequence) {
      progTag.textContent = data.sequence.running ? data.sequence.program : "IDLE";
      progTag.style.color = data.sequence.running ? "#10b981" : "var(--accent-cyan)";
    }

    const seqProgress = document.getElementById("seqProgressContainer");
    const seqBar = document.getElementById("seqProgressBar");
    if (seqProgress && seqBar && data.sequence) {
      if (data.sequence.running && data.sequence.total > 0) {
        seqProgress.style.display = "block";
        const pct = Math.min(100, Math.round((data.sequence.step / data.sequence.total) * 100));
        seqBar.style.width = `${pct}%`;
      } else {
        seqProgress.style.display = "none";
      }
    }
  }

  // --- ANIMATION LOOP (60 FPS) ---
  function animate() {
    requestAnimationFrame(animate);

    // 1. Smoothly interpolate joint angles towards target telemetry
    for (let i = 0; i < 6; i++) {
      currentQ[i] += (targetQ[i] - currentQ[i]) * 0.2;
    }

    // 2. Apply Exact Craig MDH Forward Kinematics to robot chain
    updateRobotKinematics(currentQ);

    // 3. Animate Gripper Jaws
    const targetJawPos = gripperState ? 4.0 : 18.0;
    if (jawLeft && jawRight) {
      jawLeft.position.x += (-targetJawPos - jawLeft.position.x) * 0.25;
      jawRight.position.x += (targetJawPos - jawRight.position.x) * 0.25;
    }

    // 4. Held Workpiece Visibility
    if (heldWorkpieceMesh) {
      heldWorkpieceMesh.visible = heldWorkpiece;
    }

    // 5. Update TCP Trajectory Ribbon
    if (jointGroups[5]) {
      const tipPos = new THREE.Vector3();
      jointGroups[5].getWorldPosition(tipPos);
      updateTrajectoryRibbon(tipPos);
    }

    // 6. Camera Smooth Transition
    if (camTargetPos && camTargetLook) {
      camera.position.lerp(camTargetPos, 0.08);
      controls.target.lerp(camTargetLook, 0.08);
      if (camera.position.distanceTo(camTargetPos) < 2) {
        camTargetPos = null;
        camTargetLook = null;
      }
    }

    controls.update();
    renderer.render(scene, camera);
  }

  function onWindowResize() {
    const container = document.getElementById("canvas3d");
    const width = container.clientWidth || window.innerWidth;
    const height = container.clientHeight || window.innerHeight;
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    renderer.setSize(width, height);
  }

})();
