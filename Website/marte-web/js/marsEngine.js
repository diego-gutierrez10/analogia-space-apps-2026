import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { DRACOLoader } from 'three/addons/loaders/DRACOLoader.js';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { BokehPass } from 'three/addons/postprocessing/BokehPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';

export class MarsEngine {
  constructor(container, markersOverlay, onHoverCoords) {
    this.container = container;
    this.markersOverlay = markersOverlay;
    this.infoPinsOverlay = document.getElementById('info-pins-overlay');
    this.onHoverCoords = onHoverCoords;

    this.marsGroup = new THREE.Group();
    this.routesGroup = new THREE.Group();
    this.marsGroup.add(this.routesGroup);

    this.globalMeshes = [];
    this.jezeroMeshes = [];
    this.jezeroCenterWorld = null;
    this.markers = [];
    this.infoPins = [];

    // Independent visibility flags
    this.showMarkers = true;
    this.showRoutes = true;
    this.showHistory = true;

    // Spherical orbital animation state (avoids flying through the planet)
    this.isAnimating = false;
    this.flightProgress = 0;
    this.flightDuration = 1.4; // seconds
    this.flightStartCamPos = new THREE.Vector3();
    this.flightEndCamPos = new THREE.Vector3();
    this.flightStartLookAt = new THREE.Vector3();
    this.flightEndLookAt = new THREE.Vector3();
    this.targetAperture = 0.0;

    this.surfaceRaycaster = new THREE.Raycaster();

    // Continuous orbital lighting state
    this.autoLight = true;
    this.sunAzimuthDeg = 40.0;
    this.sunOrbitSpeed = 8.5; // degrees per second
    this.clock = new THREE.Clock();
    this.targetSunPos = new THREE.Vector3(11, 4.5, 9);
    this.lightingMode = 'realistic';
    this.onSunAngleUpdate = null;

    this.initScene();
    this.initLights();
    this.initStarfield();
    this.initAtmosphere();
    this.loadModels();
    this.bindEvents();
    this.animate();
  }

  initScene() {
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x050608);

    this.camera = new THREE.PerspectiveCamera(36, window.innerWidth / window.innerHeight, 0.002, 250);
    this.camera.position.set(0, 1.8, 8.6);
    this.applyAsymmetricFraming();

    this.renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
    this.renderer.setSize(window.innerWidth, window.innerHeight);
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.08;
    this.container.appendChild(this.renderer.domElement);

    this.composer = new EffectComposer(this.renderer);
    this.composer.addPass(new RenderPass(this.scene, this.camera));

    this.bokehPass = new BokehPass(this.scene, this.camera, {
      focus: 8.6,
      aperture: 0.0,
      maxblur: 0.006
    });
    this.composer.addPass(this.bokehPass);
    this.composer.addPass(new OutputPass());

    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.05;
    this.controls.minDistance = 3.52;
    this.controls.maxDistance = 20.0;
    this.controls.zoomSpeed = 0.9;

    this.scene.add(this.marsGroup);
  }

  applyAsymmetricFraming() {
    const w = window.innerWidth;
    const h = window.innerHeight;
    if (w > 768) {
      const offsetX = w * 0.14;
      const offsetY = -h * 0.11;
      this.camera.setViewOffset(w, h, offsetX, offsetY, w, h);
    } else {
      this.camera.clearViewOffset();
    }
  }

  initLights() {
    this.sunLight = new THREE.DirectionalLight(0xffd6ad, 3.2);
    this.updateSunPositionFromDegrees(this.sunAzimuthDeg, true);
    this.scene.add(this.sunLight);

    this.hemiLight = new THREE.HemisphereLight(0xd47a52, 0x121624, 0.22);
    this.scene.add(this.hemiLight);
  }

  updateSunPositionFromDegrees(degrees, immediate = false) {
    this.sunAzimuthDeg = ((degrees % 360) + 360) % 360;
    const rad = THREE.MathUtils.degToRad(this.sunAzimuthDeg);
    this.targetSunPos.set(Math.cos(rad) * 16.0, 4.2, Math.sin(rad) * 16.0);
    if (immediate) {
      this.sunLight.position.copy(this.targetSunPos);
      this.syncSunUniforms();
    }
  }

  setAutoLight(enabled) {
    this.autoLight = enabled;
    if (this.onSunAngleUpdate) {
      this.onSunAngleUpdate(Math.round(this.sunAzimuthDeg), enabled);
    }
  }

  setLightingPreset(preset) {
    this.lightingMode = preset;
    if (preset === 'realistic') {
      this.sunLight.intensity = 3.2;
      this.hemiLight.intensity = 0.22;
    } else if (preset === 'low-angle') {
      this.sunLight.intensity = 3.7;
      this.hemiLight.intensity = 0.14;
    } else if (preset === 'zenith') {
      this.sunLight.intensity = 2.9;
      this.hemiLight.intensity = 0.34;
    }
  }

  setSunAzimuth(degrees) {
    this.autoLight = false;
    this.updateSunPositionFromDegrees(degrees, true);
    if (this.onSunAngleUpdate) {
      this.onSunAngleUpdate(Math.round(this.sunAzimuthDeg), false);
    }
  }

  syncSunUniforms() {
    const sunDir = this.sunLight.position.clone().normalize();
    if (this.atmoMat) {
      this.atmoMat.uniforms.sunDirection.value.copy(sunDir);
    }
    [...this.globalMeshes, ...this.jezeroMeshes].forEach((m) => {
      if (m.userData.elevationShader) {
        m.userData.elevationShader.uniforms.uSunDir.value.copy(sunDir);
      }
    });
  }

  initStarfield() {
    const starGeo = new THREE.BufferGeometry();
    const count = 4200;
    const positions = new Float32Array(count * 3);
    for (let i = 0; i < count * 3; i += 3) {
      const r = 90 + Math.random() * 80;
      const theta = Math.random() * Math.PI * 2;
      const phi = Math.acos(2 * Math.random() - 1);
      positions[i] = r * Math.sin(phi) * Math.cos(theta);
      positions[i + 1] = r * Math.sin(phi) * Math.sin(theta);
      positions[i + 2] = r * Math.cos(phi);
    }
    starGeo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    const starMat = new THREE.PointsMaterial({ color: 0xffffff, size: 0.2, transparent: true, opacity: 0.85 });
    this.scene.add(new THREE.Points(starGeo, starMat));
  }

  initAtmosphere() {
    const atmoGeo = new THREE.SphereGeometry(3.55, 128, 128);
    this.atmoMat = new THREE.ShaderMaterial({
      transparent: true,
      blending: THREE.AdditiveBlending,
      side: THREE.BackSide,
      depthWrite: false,
      uniforms: {
        sunDirection: { value: this.sunLight.position.clone().normalize() },
        glowColor: { value: new THREE.Color(0xFF7A42) }
      },
      vertexShader: `
        varying vec3 vNormal;
        varying vec3 vWorldPosition;
        void main() {
          vNormal = normalize(normalMatrix * normal);
          vec4 worldPos = modelMatrix * vec4(position, 1.0);
          vWorldPosition = worldPos.xyz;
          gl_Position = projectionMatrix * viewMatrix * worldPos;
        }
      `,
      fragmentShader: `
        uniform vec3 sunDirection;
        uniform vec3 glowColor;
        varying vec3 vNormal;
        varying vec3 vWorldPosition;
        void main() {
          vec3 viewDir = normalize(cameraPosition - vWorldPosition);
          float ndotv = dot(viewDir, vNormal);
          float limbBand = clamp(1.0 + ndotv, 0.0, 1.0);
          float outerFade = smoothstep(0.0, -0.42, ndotv);
          float intensity = pow(limbBand, 3.2) * outerFade * 1.15;

          vec3 worldNormal = normalize(vWorldPosition);
          float daySide = smoothstep(-0.25, 0.55, dot(worldNormal, sunDirection));
          gl_FragColor = vec4(glowColor, intensity * daySide);
        }
      `
    });
    this.scene.add(new THREE.Mesh(atmoGeo, this.atmoMat));
  }

  loadModels() {
    const dracoLoader = new DRACOLoader();
    dracoLoader.setDecoderPath('https://unpkg.com/three@0.169.0/examples/jsm/libs/draco/');

    const gltfLoader = new GLTFLoader();
    gltfLoader.setDRACOLoader(dracoLoader);

    gltfLoader.load('./models/mars_global.glb', (gltf) => {
      gltf.scene.traverse((child) => {
        if (child.isMesh) {
          child.userData.originalMaterial = child.material;
          this.globalMeshes.push(child);
        }
      });
      this.marsGroup.add(gltf.scene);
      document.getElementById('loader').classList.add('hidden');
    });

    gltfLoader.load('./models/patch_jezero.glb', (gltf) => {
      const patchScene = gltf.scene;
      this.marsGroup.add(patchScene);

      patchScene.updateMatrixWorld(true);
      const realBox = new THREE.Box3();
      patchScene.traverse((child) => {
        if (child.isMesh && child.geometry) {
          child.userData.originalMaterial = child.material;
          this.jezeroMeshes.push(child);

          child.geometry.computeBoundingBox();
          const meshBox = child.geometry.boundingBox.clone();
          meshBox.applyMatrix4(child.matrixWorld);
          realBox.union(meshBox);
        }
      });

      if (!realBox.isEmpty()) {
        this.jezeroCenterWorld = realBox.getCenter(new THREE.Vector3());
      }
    });
  }

  createHighResElevationMaterial(mesh, originalMat, radialOffset = 0.0) {
    const mat = originalMat.clone();
    mat.map = null;
    mat.color.setHex(0xffffff);
    mat.roughness = 0.88;
    mat.metalness = 0.05;

    mat.onBeforeCompile = (shader) => {
      shader.uniforms.uRadialOffset = { value: radialOffset };
      shader.uniforms.uSunDir = { value: this.sunLight.position.clone().normalize() };
      mesh.userData.elevationShader = shader;

      shader.vertexShader = `
        varying vec3 vObjPos;
        ${shader.vertexShader}
      `.replace(
        '#include <begin_vertex>',
        `
        #include <begin_vertex>
        vObjPos = position;
        `
      );

      shader.fragmentShader = `
        varying vec3 vObjPos;
        uniform float uRadialOffset;
        ${shader.fragmentShader}
      `.replace(
        '#include <color_fragment>',
        `
        #include <color_fragment>
        float trueRadius = length(vObjPos) - uRadialOffset;
        float elevNorm = clamp((trueRadius - 3.364) / 0.088, 0.0, 1.0);

        vec3 cAbyss   = vec3(0.11, 0.18, 0.38);
        vec3 cLowland = vec3(0.18, 0.44, 0.42);
        vec3 cMidland = vec3(0.45, 0.62, 0.32);
        vec3 cHigh    = vec3(0.86, 0.52, 0.20);
        vec3 cPeak    = vec3(0.96, 0.93, 0.90);

        vec3 topoColor = mix(cAbyss, cLowland, smoothstep(0.00, 0.25, elevNorm));
        topoColor = mix(topoColor, cMidland, smoothstep(0.25, 0.50, elevNorm));
        topoColor = mix(topoColor, cHigh,    smoothstep(0.50, 0.78, elevNorm));
        topoColor = mix(topoColor, cPeak,    smoothstep(0.78, 1.00, elevNorm));

        float contourFreq = 90.0;
        float distToLine = abs(fract(elevNorm * contourFreq - 0.5) - 0.5) / max(fwidth(elevNorm * contourFreq), 0.0001);
        float contourMask = 1.0 - min(distToLine, 1.0);

        float majorFreq = 18.0;
        float distMajor = abs(fract(elevNorm * majorFreq - 0.5) - 0.5) / max(fwidth(elevNorm * majorFreq), 0.0001);
        float majorMask = 1.0 - min(distMajor, 1.0);

        topoColor = mix(topoColor, topoColor * 0.72, contourMask * 0.35);
        topoColor = mix(topoColor, topoColor * 0.52, majorMask * 0.55);

        diffuseColor.rgb = topoColor;
        `
      );
    };

    mat.needsUpdate = true;
    return mat;
  }

  setViewMode(mode) {
    if (mode === 'natural') {
      this.globalMeshes.forEach((m) => {
        if (m.userData.originalMaterial) m.material = m.userData.originalMaterial;
      });
      this.jezeroMeshes.forEach((m) => {
        if (m.userData.originalMaterial) m.material = m.userData.originalMaterial;
      });
    } else if (mode === 'elevation') {
      this.globalMeshes.forEach((m) => {
        if (!m.userData.elevationMaterial && m.userData.originalMaterial) {
          m.userData.elevationMaterial = this.createHighResElevationMaterial(m, m.userData.originalMaterial, 0.0);
        }
        if (m.userData.elevationMaterial) m.material = m.userData.elevationMaterial;
      });

      this.jezeroMeshes.forEach((m) => {
        if (!m.userData.elevationMaterial && m.userData.originalMaterial) {
          m.userData.elevationMaterial = this.createHighResElevationMaterial(m, m.userData.originalMaterial, 0.0005);
        }
        if (m.userData.elevationMaterial) m.material = m.userData.elevationMaterial;
      });
    }
  }

  latLonToVector3(latDeg, lonDeg, radius = 3.405) {
    const phi = THREE.MathUtils.degToRad(lonDeg - 180.0);
    const theta = THREE.MathUtils.degToRad(90.0 - latDeg);
    return new THREE.Vector3(
      radius * Math.sin(theta) * Math.cos(phi),
      radius * Math.cos(theta),
      -radius * Math.sin(theta) * Math.sin(phi)
    );
  }

  getSurfacePointAt(latDeg, lonDeg, elevationOffset = 0.001) {
    const dir = this.latLonToVector3(latDeg, lonDeg, 1.0).normalize();
    const origin = dir.clone().multiplyScalar(4.0);
    this.surfaceRaycaster.set(origin, dir.clone().negate());

    const targets = [...this.jezeroMeshes, ...this.globalMeshes];
    const hits = this.surfaceRaycaster.intersectObjects(targets, false);
    if (hits.length > 0) {
      return hits[0].point.clone().add(dir.multiplyScalar(elevationOffset));
    }
    return this.latLonToVector3(latDeg, lonDeg, 3.405);
  }

  renderRoutesAndHistory(locations, onSelectHistory) {
    while (this.routesGroup.children.length > 0) {
      this.routesGroup.remove(this.routesGroup.children[0]);
    }
    if (this.infoPinsOverlay) this.infoPinsOverlay.innerHTML = '';
    this.infoPins = [];

    locations.forEach((loc) => {
      if (loc.rover_traverse && loc.rover_traverse.waypoints?.length > 1) {
        const points3D = loc.rover_traverse.waypoints.map((wp) =>
          this.getSurfacePointAt(wp.lat, wp.lon, 0.0008)
        );
        const curve = new THREE.CatmullRomCurve3(points3D);
        const sampledPoints = curve.getPoints(80);
        const lineGeo = new THREE.BufferGeometry().setFromPoints(sampledPoints);
        const lineMat = new THREE.LineBasicMaterial({
          color: new THREE.Color(loc.rover_traverse.color || '#FF7138'),
          transparent: true,
          opacity: 0.95
        });
        const traverseLine = new THREE.Line(lineGeo, lineMat);
        this.routesGroup.add(traverseLine);

        const dotGeo = new THREE.SphereGeometry(0.00028, 12, 12);
        const dotMat = new THREE.MeshBasicMaterial({
          color: new THREE.Color(loc.rover_traverse.color || '#FFFFFF')
        });
        points3D.forEach((pt) => {
          const dot = new THREE.Mesh(dotGeo, dotMat);
          dot.position.copy(pt);
          this.routesGroup.add(dot);
        });
      }

      if (loc.historical_events && this.infoPinsOverlay) {
        loc.historical_events.forEach((ev) => {
          const pos3D = this.getSurfacePointAt(ev.latitude, ev.longitude, 0.0012);
          const btn = document.createElement('button');
          btn.className = 'info-pin-3d';
          btn.textContent = 'i';
          btn.title = ev.title;
          btn.addEventListener('click', (e) => {
            e.stopPropagation();
            document.querySelectorAll('.info-pin-3d').forEach((p) => p.classList.remove('active'));
            btn.classList.add('active');
            onSelectHistory(ev, loc);
          });
          this.infoPinsOverlay.appendChild(btn);
          this.infoPins.push({ pos3D, el: btn });
        });
      }
    });
  }

  setMarkers(locations, onSelectLocation) {
    this.markersOverlay.innerHTML = '';
    this.markers = [];

    locations.forEach((loc) => {
      const pos3D = (loc.id === 'jezero-crater' && this.jezeroCenterWorld)
        ? this.jezeroCenterWorld.clone()
        : this.latLonToVector3(loc.latitude, loc.longitude);

      const el = document.createElement('div');
      el.className = 'mars-pin';
      el.innerHTML = `<div class="mars-pin-label">${loc.name}</div><div class="mars-pin-dot"></div>`;
      el.addEventListener('click', (e) => {
        e.stopPropagation();
        onSelectLocation(loc);
      });
      this.markersOverlay.appendChild(el);

      this.markers.push({ pos3D, el });
    });
  }

  updateProjectedMarkers() {
    const camDir = this.camera.position.clone().normalize();

    if (!this.showMarkers) {
      this.markersOverlay.style.display = 'none';
    } else {
      this.markersOverlay.style.display = 'block';
      this.markers.forEach((m) => {
        const pinDir = m.pos3D.clone().normalize();
        if (pinDir.dot(camDir) < 0.15) {
          m.el.style.opacity = '0';
          m.el.style.pointerEvents = 'none';
          return;
        }
        const projected = m.pos3D.clone().project(this.camera);
        const x = (projected.x * 0.5 + 0.5) * window.innerWidth;
        const y = (-projected.y * 0.5 + 0.5) * window.innerHeight;

        m.el.style.opacity = '1';
        m.el.style.pointerEvents = 'auto';
        m.el.style.left = `${x}px`;
        m.el.style.top = `${y}px`;
      });
    }

    this.routesGroup.visible = this.showRoutes;

    if (!this.showHistory) {
      if (this.infoPinsOverlay) this.infoPinsOverlay.style.display = 'none';
    } else if (this.infoPinsOverlay) {
      this.infoPinsOverlay.style.display = 'block';
      this.infoPins.forEach((pin) => {
        const pinDir = pin.pos3D.clone().normalize();
        if (pinDir.dot(camDir) < 0.2) {
          pin.el.style.opacity = '0';
          pin.el.style.pointerEvents = 'none';
          return;
        }
        const projected = pin.pos3D.clone().project(this.camera);
        const x = (projected.x * 0.5 + 0.5) * window.innerWidth;
        const y = (-projected.y * 0.5 + 0.5) * window.innerHeight;

        pin.el.style.opacity = '1';
        pin.el.style.pointerEvents = 'auto';
        pin.el.style.left = `${x}px`;
        pin.el.style.top = `${y}px`;
      });
    }
  }

  startOrbitalFlight(endCamPos, endLookAt, duration = 1.45) {
    this.flightStartCamPos.copy(this.camera.position);
    this.flightEndCamPos.copy(endCamPos);
    this.flightStartLookAt.copy(this.controls.target);
    this.flightEndLookAt.copy(endLookAt);
    this.flightProgress = 0;
    this.flightDuration = duration;
    this.isAnimating = true;
  }

  flyToGlobal() {
    this.controls.minDistance = 3.52;
    this.targetAperture = 0.0;
    this.startOrbitalFlight(new THREE.Vector3(0, 1.8, 8.6), new THREE.Vector3(0, 0, 0), 1.3);
  }

  flyToLocation(loc) {
    const targetPoint = (loc.id === 'jezero-crater' && this.jezeroCenterWorld)
      ? this.jezeroCenterWorld.clone()
      : this.latLonToVector3(loc.latitude, loc.longitude);

    // 1. Compute the exact angle to the selected point and STOP the sun rotation
    // so the location stays lit while it is explored
    const sunAngleToTarget = THREE.MathUtils.radToDeg(
      Math.atan2(targetPoint.z, targetPoint.x)
    );
    const optimalAzimuth = (sunAngleToTarget + 35 + 360) % 360;

    this.autoLight = false; // Stays fixed when a site is selected
    this.updateSunPositionFromDegrees(optimalAzimuth, false);
    if (this.onSunAngleUpdate) {
      this.onSunAngleUpdate(Math.round(optimalAzimuth), false);
    }

    // 2. Set up the camera and spherical orbital flight (without crossing the planet)
    this.controls.minDistance = loc.has_hd_patch ? 0.05 : 0.45;
    this.targetAperture = loc.has_hd_patch ? 0.0038 : 0.0;

    const normalDir = targetPoint.clone().normalize();
    const tangent = new THREE.Vector3().crossVectors(new THREE.Vector3(0, 1, 0), normalDir).normalize();
    const dist = loc.has_hd_patch ? 0.24 : 1.15;

    const endCamPos = targetPoint.clone()
      .add(normalDir.multiplyScalar(dist))
      .add(tangent.multiplyScalar(dist * 0.65));

    this.startOrbitalFlight(endCamPos, targetPoint.clone(), 1.5);
  }

  stepZoom(factor) {
    const dir = this.camera.position.clone().sub(this.controls.target);
    dir.multiplyScalar(factor);
    const newDist = Math.max(this.controls.minDistance, Math.min(this.controls.maxDistance, dir.length()));
    dir.setLength(newDist);
    this.camera.position.copy(this.controls.target).add(dir);
  }

  bindEvents() {
    this.controls.addEventListener('start', () => { this.isAnimating = false; });

    window.addEventListener('resize', () => {
      this.camera.aspect = window.innerWidth / window.innerHeight;
      this.applyAsymmetricFraming();
      this.camera.updateProjectionMatrix();
      this.renderer.setSize(window.innerWidth, window.innerHeight);
      this.composer.setSize(window.innerWidth, window.innerHeight);
    });
  }

  animate() {
    requestAnimationFrame(() => this.animate());
    const delta = this.clock.getDelta();

    // Spherical orbital flight around Mars (keeps a safe radius > 3.65 while turning)
    if (this.isAnimating) {
      this.flightProgress = Math.min(1.0, this.flightProgress + delta / this.flightDuration);
      // Smooth easeInOutCubic curve
      const t = this.flightProgress < 0.5
        ? 4 * this.flightProgress * this.flightProgress * this.flightProgress
        : 1 - Math.pow(-2 * this.flightProgress + 2, 3) / 2;

      // Spherical angular interpolation of direction + orbital elevation arc
      const startLen = this.flightStartCamPos.length();
      const endLen = this.flightEndCamPos.length();
      const startDir = this.flightStartCamPos.clone().normalize();
      const endDir = this.flightEndCamPos.clone().normalize();

      const angleBetween = startDir.angleTo(endDir);
      const qStart = new THREE.Quaternion().setFromUnitVectors(new THREE.Vector3(0, 0, 1), startDir);
      const qEnd = new THREE.Quaternion().setFromUnitVectors(new THREE.Vector3(0, 0, 1), endDir);
      qStart.slerp(qEnd, t);
      const currentDir = new THREE.Vector3(0, 0, 1).applyQuaternion(qStart);

      // For large angular distances around the globe, raise the orbit so it does not graze the crust
      const baseRadius = THREE.MathUtils.lerp(startLen, endLen, t);
      const arcBoost = Math.sin(Math.PI * t) * Math.min(3.2, angleBetween * 1.35);
      const safeRadius = Math.max(3.55, baseRadius + arcBoost);

      this.camera.position.copy(currentDir.multiplyScalar(safeRadius));
      this.controls.target.lerpVectors(this.flightStartLookAt, this.flightEndLookAt, t);

      if (this.flightProgress >= 1.0) {
        this.camera.position.copy(this.flightEndCamPos);
        this.controls.target.copy(this.flightEndLookAt);
        this.isAnimating = false;
      }
    }

    // Continuous automatic sun rotation (only when autoLight = true)
    if (this.autoLight) {
      this.sunAzimuthDeg = (this.sunAzimuthDeg + this.sunOrbitSpeed * delta) % 360;
      const rad = THREE.MathUtils.degToRad(this.sunAzimuthDeg);
      this.targetSunPos.set(Math.cos(rad) * 16.0, 4.2, Math.sin(rad) * 16.0);
      if (this.onSunAngleUpdate) {
        this.onSunAngleUpdate(Math.round(this.sunAzimuthDeg), true);
      }
    }

    if (this.sunLight.position.distanceTo(this.targetSunPos) > 0.005) {
      this.sunLight.position.lerp(this.targetSunPos, 0.14);
      this.syncSunUniforms();
    }

    const currentAp = this.bokehPass.uniforms['aperture'].value;
    this.bokehPass.uniforms['aperture'].value += (this.targetAperture - currentAp) * 0.07;
    this.bokehPass.uniforms['focus'].value = this.camera.position.distanceTo(this.controls.target);

    this.controls.update();
    this.updateProjectedMarkers();
    this.composer.render();
  }
}