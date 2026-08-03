/* Minimal force-directed graph renderer.
 *
 * Hand-rolled rather than vendored: a strict local-only origin rules out a CDN,
 * and bundling a full graph library would add hundreds of kilobytes for the one
 * layout this dashboard needs. Barnes-Hut is overkill at a few hundred nodes, so
 * repulsion is computed pairwise on a capped node set.
 */

export function createForceGraph(svg, options = {}) {
  const NS = 'http://www.w3.org/2000/svg';
  const cfg = {
    charge: -900,
    linkDistance: 70,
    linkStrength: 0.035,
    centerStrength: 0.006,
    damping: 0.86,
    minAlpha: 0.002,
    labelThreshold: 1.1,
    ...options,
  };

  let nodes = [];
  let edges = [];
  let alpha = 0;
  let raf = null;
  let selected = null;
  const view = { x: 0, y: 0, k: 1 };
  const listeners = { select: [], labels: [] };

  const gRoot = document.createElementNS(NS, 'g');
  const gEdges = document.createElementNS(NS, 'g');
  const gNodes = document.createElementNS(NS, 'g');
  const gLabels = document.createElementNS(NS, 'g');
  gRoot.append(gEdges, gNodes, gLabels);
  svg.append(gRoot);

  function size() {
    const r = svg.getBoundingClientRect();
    return { w: r.width || 900, h: r.height || 600 };
  }

  function applyTransform() {
    gRoot.setAttribute('transform', `translate(${view.x},${view.y}) scale(${view.k})`);
    // Labels are per-node now (see layoutLabels); the group stays visible and
    // collision decides which ones render. Zooming changes what fits, so the
    // placement is recomputed once the interaction settles.
    gLabels.setAttribute('opacity', '1');
    scheduleLabels();
  }

  /** Seed positions on a circle so the layout unfolds instead of exploding. */
  function seed() {
    const { w, h } = size();
    nodes.forEach((n, i) => {
      const a = (i / Math.max(nodes.length, 1)) * Math.PI * 2;
      const r = Math.min(w, h) * 0.34;
      n.x = w / 2 + Math.cos(a) * r * (0.55 + ((i * 37) % 45) / 100);
      n.y = h / 2 + Math.sin(a) * r * (0.55 + ((i * 53) % 45) / 100);
      n.vx = 0;
      n.vy = 0;
    });
  }

  function step() {
    const { w, h } = size();
    const cx = w / 2;
    const cy = h / 2;

    for (let i = 0; i < nodes.length; i++) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j++) {
        const b = nodes[j];
        let dx = b.x - a.x;
        let dy = b.y - a.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 0.01) {
          // Identical positions have no direction to separate along; nudge
          // deterministically so the layout stays reproducible across reloads.
          dx = ((i % 7) - 3) * 0.1 || 0.1;
          dy = ((j % 7) - 3) * 0.1 || 0.1;
          d2 = dx * dx + dy * dy;
        }
        const dist = Math.sqrt(d2);
        const f = cfg.charge / d2;
        const fx = (dx / dist) * f;
        const fy = (dy / dist) * f;
        a.vx += fx;
        a.vy += fy;
        b.vx -= fx;
        b.vy -= fy;
      }
    }

    for (const e of edges) {
      const a = e.a;
      const b = e.b;
      if (!a || !b) continue;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const f = (dist - cfg.linkDistance) * cfg.linkStrength;
      const fx = (dx / dist) * f;
      const fy = (dy / dist) * f;
      a.vx += fx;
      a.vy += fy;
      b.vx -= fx;
      b.vy -= fy;
    }

    for (const n of nodes) {
      n.vx += (cx - n.x) * cfg.centerStrength;
      n.vy += (cy - n.y) * cfg.centerStrength;
      if (n.fixed) {
        n.vx = 0;
        n.vy = 0;
        continue;
      }
      n.vx *= cfg.damping;
      n.vy *= cfg.damping;
      n.x += n.vx * alpha;
      n.y += n.vy * alpha;
    }
  }

  function draw() {
    for (const e of edges) {
      if (!e.el || !e.a || !e.b) continue;
      e.el.setAttribute('x1', e.a.x);
      e.el.setAttribute('y1', e.a.y);
      e.el.setAttribute('x2', e.b.x);
      e.el.setAttribute('y2', e.b.y);
    }
    for (const n of nodes) {
      if (n.el) {
        n.el.setAttribute('cx', n.x);
        n.el.setAttribute('cy', n.y);
      }
      if (n.halo) {
        n.halo.setAttribute('cx', n.x);
        n.halo.setAttribute('cy', n.y);
      }
      if (n.labelEl) {
        n.labelEl.setAttribute('x', n.x + n.r + 3);
        n.labelEl.setAttribute('y', n.y + 3);
      }
    }
  }

  /* Hide labels that would overlap one already shown.
   *
   * Without this, expanding a few hundred nodes produces a wall of text where
   * every label sits on top of its neighbours and none of them is readable - the
   * graph looks busy but conveys less than no labels at all. Nodes are considered
   * biggest-first, so hubs keep their label and leaves give theirs up. Screen
   * space, not graph space: what matters is whether they collide as drawn.
   */
  /* Measured label width in unscaled user units, cached on the node.
   *
   * Measured rather than estimated: the font is proportional, so a fixed
   * per-character average is wrong in both directions - "CLAUDE.md" measures
   * 56px against a 49px estimate, "api-design-reviewer" 93px against 103px.
   * Under-estimates are the damaging direction, because they let overlapping
   * labels pass the collision test.
   *
   * Measured lazily: an SVG inside a hidden container reports zero, and the
   * graph is built while its tab is still hidden.
   */
  function labelWidth(n) {
    if (n.labelW > 0) return n.labelW;
    try {
      const w = n.labelEl.getComputedTextLength();
      if (w > 0) {
        n.labelW = w;
        return w;
      }
    } catch {
      /* not rendered yet */
    }
    return String(n.label).length * 5.4; // fallback until it can be measured
  }

  function layoutLabels() {
    const placed = [];
    const order = nodes
      .filter((n) => n.labelEl)
      .slice()
      .sort((a, b) => b.r - a.r || String(a.label).localeCompare(String(b.label)));

    // The label elements live inside the zoomed group, so their rendered size
    // scales with `view.k`. Measuring them at a fixed pixel width made every box
    // too small as you zoomed in, which is exactly when the collisions became
    // visible: the check passed while the text overlapped on screen.
    const lineH = 11 * view.k;
    const pad = 3 * view.k; // a little breathing room so labels do not touch
    const { w, h } = size();

    for (const n of order) {
      // Screen-space box for this label, from the measured width.
      const sx = n.x * view.k + view.x + (n.r + 3) * view.k;
      const sy = n.y * view.k + view.y;
      const bw = labelWidth(n) * view.k + pad;
      const box = { x: sx, y: sy - lineH / 2, w: bw, h: lineH };

      // Off-screen labels cost nothing to hide and free space for the rest.
      const offscreen = box.x > w + 40 || box.x + box.w < -40 || box.y > h + 40 || box.y + box.h < -40;
      let hit = offscreen;
      if (!hit) {
        for (const p of placed) {
          if (box.x < p.x + p.w && box.x + box.w > p.x && box.y < p.y + p.h && box.y + box.h > p.y) {
            hit = true;
            break;
          }
        }
      }
      if (hit) {
        n.labelEl.setAttribute('opacity', '0');
      } else {
        n.labelEl.setAttribute('opacity', '1');
        placed.push(box);
      }
    }
    return { shown: placed.length, total: order.length };
  }

  let labelTimer = null;
  function scheduleLabels() {
    if (labelTimer) clearTimeout(labelTimer);
    labelTimer = setTimeout(() => {
      labelTimer = null;
      const stats = layoutLabels();
      listeners.labels.forEach((fn) => fn(stats));
    }, 120);
  }

  function tick() {
    step();
    draw();
    alpha *= 0.985;
    if (alpha > cfg.minAlpha) {
      raf = requestAnimationFrame(tick);
    } else {
      raf = null;
      // Settle first, then place labels once: doing it per frame at several
      // hundred nodes costs more than the layout itself.
      scheduleLabels();
    }
  }

  function kick(a = 1) {
    alpha = a;
    if (!raf) raf = requestAnimationFrame(tick);
  }

  function select(node) {
    if (selected && selected.el) selected.el.classList.remove('selected');
    selected = node;
    if (node && node.el) node.el.classList.add('selected');
    listeners.select.forEach((fn) => fn(node));
  }

  function render(data) {
    gEdges.replaceChildren();
    gNodes.replaceChildren();
    gLabels.replaceChildren();

    nodes = data.nodes.map((n) => ({ ...n }));
    const byId = new Map(nodes.map((n) => [n.id, n]));
    edges = data.edges
      .map((e) => ({ ...e, a: byId.get(e.source), b: byId.get(e.target) }))
      .filter((e) => e.a && e.b);

    seed();

    for (const e of edges) {
      const line = document.createElementNS(NS, 'line');
      line.setAttribute('class', `edge ${e.kind}`);
      e.el = line;
      gEdges.append(line);
    }

    for (const n of nodes) {
      n.r = cfg.radius ? cfg.radius(n) : 6;
      if (cfg.flag && cfg.flag(n)) {
        const halo = document.createElementNS(NS, 'circle');
        halo.setAttribute('class', 'halo');
        halo.setAttribute('r', n.r + 3.5);
        n.halo = halo;
        gNodes.append(halo);
      }
      const c = document.createElementNS(NS, 'circle');
      c.setAttribute('class', 'node');
      c.setAttribute('r', n.r);
      c.setAttribute('fill', cfg.color ? cfg.color(n) : 'currentColor');
      // A node whose label lost the overlap contest is otherwise unidentifiable
      // without zooming until it fits. A native <title> costs nothing, needs no
      // script, and answers "what is this dot?" on hover for every node -
      // including the ones that never get a drawn label.
      const tip = document.createElementNS(NS, 'title');
      tip.textContent = n.label;
      c.append(tip);
      c.addEventListener('pointerdown', (ev) => startDrag(ev, n));
      c.addEventListener('click', (ev) => {
        ev.stopPropagation();
        select(n);
      });
      n.el = c;
      gNodes.append(c);

      const t = document.createElementNS(NS, 'text');
      t.textContent = n.label;
      // Keep the text element under a distinct key: `n.label` stays the string,
      // which the detail panel and the search filter both read.
      n.labelEl = t;
      gLabels.append(t);
    }



    // Position everything synchronously before starting the simulation.
    // requestAnimationFrame is suspended in a background tab, so a graph built
    // there would otherwise have no coordinates at all until the tab is focused:
    // every element sat at the origin. Drawing once up front means the layout is
    // always valid, and the simulation only improves it.
    draw();
    applyTransform();
    layoutLabels();
    kick(1);
  }

  /* ---- interaction ---------------------------------------------------- */

  let drag = null;

  function svgPoint(ev) {
    const r = svg.getBoundingClientRect();
    return {
      x: (ev.clientX - r.left - view.x) / view.k,
      y: (ev.clientY - r.top - view.y) / view.k,
    };
  }

  function startDrag(ev, node) {
    ev.preventDefault();
    ev.stopPropagation();
    const p = svgPoint(ev);
    drag = { node, dx: node.x - p.x, dy: node.y - p.y };
    node.fixed = true;
    svg.setPointerCapture(ev.pointerId);
  }

  let pan = null;
  svg.addEventListener('pointerdown', (ev) => {
    if (drag) return;
    pan = { x: ev.clientX, y: ev.clientY, ox: view.x, oy: view.y };
    svg.classList.add('dragging');
    svg.setPointerCapture(ev.pointerId);
  });

  svg.addEventListener('pointermove', (ev) => {
    if (drag) {
      const p = svgPoint(ev);
      drag.node.x = p.x + drag.dx;
      drag.node.y = p.y + drag.dy;
      draw();
      kick(0.35);
      return;
    }
    if (pan) {
      view.x = pan.ox + (ev.clientX - pan.x);
      view.y = pan.oy + (ev.clientY - pan.y);
      applyTransform();
    }
  });

  function endPointer() {
    if (drag) {
      drag.node.fixed = false;
      drag = null;
      kick(0.3);
    }
    pan = null;
    svg.classList.remove('dragging');
  }
  svg.addEventListener('pointerup', endPointer);
  svg.addEventListener('pointercancel', endPointer);
  svg.addEventListener('click', (ev) => {
    if (ev.target === svg) select(null);
  });

  svg.addEventListener(
    'wheel',
    (ev) => {
      ev.preventDefault();
      const r = svg.getBoundingClientRect();
      const mx = ev.clientX - r.left;
      const my = ev.clientY - r.top;
      const factor = Math.exp(-ev.deltaY * 0.0015);
      const k = Math.min(4, Math.max(0.25, view.k * factor));
      view.x = mx - ((mx - view.x) * k) / view.k;
      view.y = my - ((my - view.y) * k) / view.k;
      view.k = k;
      applyTransform();
    },
    { passive: false },
  );

  return {
    render,
    kick,
    select,
    on(event, fn) {
      if (listeners[event]) listeners[event].push(fn);
    },
    relayoutLabels: scheduleLabels,
    /* Scale and centre the view on everything currently rendered.
     *
     * Focusing on a neighbourhood leaves eight nodes in the middle of a canvas
     * sized for two hundred, which wastes the space that made the view readable
     * in the first place. Called after the layout settles, so it fits where the
     * nodes ended up rather than where they started.
     */
    fit(padding = 40) {
      if (!nodes.length) return;
      const xs = nodes.map((n) => n.x);
      const ys = nodes.map((n) => n.y);
      const minX = Math.min(...xs);
      const maxX = Math.max(...xs);
      const minY = Math.min(...ys);
      const maxY = Math.max(...ys);
      const { w, h } = size();
      if (!w || !h) return;
      const bw = Math.max(maxX - minX, 1);
      const bh = Math.max(maxY - minY, 1);
      // Capped: a two-node neighbourhood should not fill the screen with two
      // enormous circles.
      view.k = Math.min(2.2, (w - padding * 2) / bw, (h - padding * 2) / bh);
      view.x = w / 2 - ((minX + maxX) / 2) * view.k;
      view.y = h / 2 - ((minY + maxY) / 2) * view.k;
      applyTransform();
      scheduleLabels();
    },
    reset() {
      view.x = 0;
      view.y = 0;
      view.k = 1;
      applyTransform();
      seed();
      kick(1);
    },
    get selected() {
      return selected;
    },
  };
}
