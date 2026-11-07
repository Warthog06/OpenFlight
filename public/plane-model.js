/* A simple low-poly airliner for the 3D view, built in code so there is no model file to load.
 *
 * Units are metres (about 40 m long). Axes: nose along +x, left wing along +y, up along +z,
 * which is what deck.gl's SimpleMeshLayer expects (yaw 0 = nose pointing east).
 * Every triangle gets its own flat normal, pointing away from the centre of the part it belongs
 * to, which gives a clean faceted look under deck.gl's lighting.
 */
'use strict';

window.OpenFlightPlane = (() => {
  const positions = [];
  const normals = [];

  const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
  const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
  const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];

  // One triangle, with its normal flipped if needed so it faces away from `inside`.
  function tri(a, b, c, inside) {
    let n = cross(sub(b, a), sub(c, a));
    const len = Math.hypot(n[0], n[1], n[2]);
    if (len < 1e-9) return;
    n = n.map((v) => v / len);
    const mid = [(a[0] + b[0] + c[0]) / 3, (a[1] + b[1] + c[1]) / 3, (a[2] + b[2] + c[2]) / 3];
    if (dot(n, sub(mid, inside)) < 0) { n = n.map((v) => -v); [b, c] = [c, b]; }
    positions.push(...a, ...b, ...c);
    normals.push(...n, ...n, ...n);
  }
  const quad = (a, b, c, d, inside) => { tri(a, b, c, inside); tri(a, c, d, inside); };

  // A closed slab from a top and bottom outline of four corners each (wings, tail surfaces, fin).
  function slab(top, bottom) {
    const all = top.concat(bottom);
    const inside = [0, 1, 2].map((k) => all.reduce((s, p) => s + p[k], 0) / all.length);
    quad(top[0], top[1], top[2], top[3], inside);
    quad(bottom[0], bottom[1], bottom[2], bottom[3], inside);
    for (let i = 0; i < 4; i++) {
      const j = (i + 1) % 4;
      quad(top[i], top[j], bottom[j], bottom[i], inside);
    }
  }

  // A body of revolution along x: stations are [x, radius, zCentre]; radius 0 makes a point.
  function body(stations, sides, yCentre = 0) {
    const ring = ([x, r, z]) => Array.from({ length: sides }, (_, i) => {
      const a = (i / sides) * Math.PI * 2;
      return [x, yCentre + r * Math.cos(a), z + r * Math.sin(a)];
    });
    const rings = stations.map(ring);
    for (let s = 0; s < stations.length - 1; s++) {
      const inside = [(stations[s][0] + stations[s + 1][0]) / 2, yCentre, (stations[s][2] + stations[s + 1][2]) / 2];
      for (let i = 0; i < sides; i++) {
        const j = (i + 1) % sides;
        quad(rings[s][i], rings[s][j], rings[s + 1][j], rings[s + 1][i], inside);
      }
    }
    // close the ends that are not points
    for (const s of [0, stations.length - 1]) {
      if (stations[s][1] === 0) continue;
      const c = [stations[s][0], yCentre, stations[s][2]];
      const inside = [stations[s][0] + (s === 0 ? -1 : 1), yCentre, stations[s][2]];
      for (let i = 0; i < sides; i++) tri(c, rings[s][i], rings[s][(i + 1) % sides], inside);
    }
  }

  // Fuselage: rounded nose, straight cabin, tail cone sweeping up to the fin.
  body([
    [20.0, 0.0, 0.0],
    [19.0, 0.9, -0.1],
    [17.0, 1.7, 0.0],
    [14.5, 2.0, 0.0],
    [-11.0, 2.0, 0.0],
    [-16.5, 1.1, 0.7],
    [-20.0, 0.25, 1.3],
  ], 10);

  // Main wings: swept back, slight dihedral, thinner at the tips.
  for (const side of [1, -1]) {
    const y0 = 1.6 * side, y1 = 17.5 * side;
    slab(
      [[4.5, y0, 0.15], [-4.0, y0, 0.15], [-9.5, y1, 1.35], [-6.8, y1, 1.35]],
      [[4.5, y0, -0.45], [-4.0, y0, -0.45], [-9.5, y1, 1.2], [-6.8, y1, 1.2]],
    );
    // engine under each wing
    body([[4.2, 0.0, -1.35], [3.6, 0.95, -1.35], [-1.6, 0.95, -1.35], [-2.6, 0.55, -1.35]], 8, 6.2 * side);
    // horizontal tailplane
    const t0 = 0.7 * side, t1 = 7.0 * side;
    slab(
      [[-14.6, t0, 1.05], [-18.9, t0, 1.05], [-20.2, t1, 1.6], [-18.3, t1, 1.6]],
      [[-14.6, t0, 0.75], [-18.9, t0, 0.75], [-20.2, t1, 1.45], [-18.3, t1, 1.45]],
    );
  }

  // Vertical fin.
  slab(
    [[-13.2, 0.18, 1.6], [-19.4, 0.18, 1.6], [-20.6, 0.08, 8.6], [-18.1, 0.08, 8.6]],
    [[-13.2, -0.18, 1.6], [-19.4, -0.18, 1.6], [-20.6, -0.08, 8.6], [-18.1, -0.08, 8.6]],
  );

  return {
    length: 40,
    mesh: {
      positions: { value: new Float32Array(positions), size: 3 },
      normals: { value: new Float32Array(normals), size: 3 },
    },
    triangles: positions.length / 9,
  };
})();
