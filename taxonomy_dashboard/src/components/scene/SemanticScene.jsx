import { useRef, useEffect, useMemo, useCallback } from 'react'
import useStore from '../../store/useStore.js'
import { buildSpatialLayout, seededRand } from './sceneUtils.js'

// ── Constants ──────────────────────────────────────────────────────────────────
const BG2D = '#080e1a'
const BG3D = '#030610'

// ── Helpers ────────────────────────────────────────────────────────────────────
function w2c(wx, wy, w, h, tx, ty, sc) {
  return [w / 2 + wx * sc + tx, h / 2 - wy * sc + ty]
}
function c2w(cx, cy, w, h, tx, ty, sc) {
  return [(cx - w / 2 - tx) / sc, -(cy - h / 2 - ty) / sc]
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value))
}

function shortLabel(value, max = 28) {
  const text = String(value || '').trim()
  return text.length > max ? text.slice(0, max) + '…' : text
}

function fitAll(cls, w, h) {
  if (!cls.length) return { tx: 0, ty: 0, sc: 5 }
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity
  for (const c of cls) {
    x0 = Math.min(x0, c._pos[0]); x1 = Math.max(x1, c._pos[0])
    y0 = Math.min(y0, c._pos[1]); y1 = Math.max(y1, c._pos[1])
  }
  const sc = Math.min(w * 0.80 / ((x1 - x0) || 1), h * 0.80 / ((y1 - y0) || 1), 60)
  return { tx: -((x0 + x1) / 2) * sc, ty: ((y0 + y1) / 2) * sc, sc }
}

function nearest2D(cls, wx, wy, hitWU) {
  let best = null, bd2 = hitWU * hitWU
  for (const c of cls) {
    const dx = c._pos[0] - wx, dy = c._pos[1] - wy
    const d2 = dx * dx + dy * dy
    if (d2 < bd2) { bd2 = d2; best = c }
  }
  return best
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath()
  ctx.moveTo(x + r, y); ctx.lineTo(x + w - r, y)
  ctx.arcTo(x + w, y, x + w, y + r, r); ctx.lineTo(x + w, y + h - r)
  ctx.arcTo(x + w, y + h, x + w - r, y + h, r); ctx.lineTo(x + r, y + h)
  ctx.arcTo(x, y + h, x, y + h - r, r); ctx.lineTo(x, y + r)
  ctx.arcTo(x, y, x + r, y, r); ctx.closePath()
}


function drawDiamond(ctx, x, y, r) {
  ctx.beginPath()
  ctx.moveTo(x, y - r)
  ctx.lineTo(x + r, y)
  ctx.lineTo(x, y + r)
  ctx.lineTo(x - r, y)
  ctx.closePath()
}

function selectedMedoidOffset(cluster, scale = 1) {
  const simRaw = Number(cluster?.medoid_similarity_to_centroid ?? cluster?.medoidSimilarity ?? 0.82)
  const sim = Number.isFinite(simRaw) ? Math.max(0, Math.min(1, simRaw)) : 0.82
  const gap = (10 + (1 - sim) * 58) * scale
  const seed = String(cluster?.cluster_id || cluster?.id || '').split('').reduce((n, ch) => n + ch.charCodeAt(0), 0)
  const angle = (seed % 628) / 100
  return { dx: Math.cos(angle) * gap, dy: Math.sin(angle) * gap, sim }
}

function drawTooltip(ctx, c, px, py, w, h) {
  const title = c.display_name || c.medoid_label || c.cluster_id || 'Unnamed'
  const titleT = title.length > 36 ? title.slice(0, 36) + '…' : title
  const prod = c.production_hit_count ? ` · ${Number(c.production_hit_count).toLocaleString()} production hits` : ''
  const sub = `${(c.cluster_size || 0).toLocaleString()} items · ${c.field_name || ''}${prod}`
  ctx.save()
  ctx.font = '600 11px Inter,system-ui,sans-serif'
  const tw = Math.max(ctx.measureText(titleT).width, ctx.measureText(sub).width)
  const bw = tw + 26, bh = 50, br = 6
  let bx = px + 14, by = py - bh - 10
  if (bx + bw > w - 8) bx = px - bw - 14
  if (by < 8) by = py + 12
  roundRect(ctx, bx, by, bw, bh, br)
  ctx.fillStyle = 'rgba(6,10,20,0.97)'; ctx.fill()
  ctx.strokeStyle = 'rgba(255,255,255,0.09)'; ctx.lineWidth = 1; ctx.stroke()
  const accent = c._fieldColor || c._color || '#6366f1'
  roundRect(ctx, bx, by, 3, bh, [br, 0, 0, br])
  ctx.fillStyle = accent; ctx.fill()
  ctx.font = 'bold 8px Inter,system-ui,sans-serif'
  ctx.fillStyle = accent
  ctx.fillText((c.field_name || '').toUpperCase(), bx + 12, by + 13)
  ctx.font = '600 11px Inter,system-ui,sans-serif'
  ctx.fillStyle = '#e2e8f0'
  ctx.fillText(titleT, bx + 12, by + 29)
  ctx.font = '400 9.5px Inter,system-ui,sans-serif'
  ctx.fillStyle = '#64748b'
  ctx.fillText(sub, bx + 12, by + 43)
  ctx.restore()
}

// ── 2D draw ────────────────────────────────────────────────────────────────────
function draw2D(ctx, cls, w, h, tx, ty, sc, selId, hovId, showL) {
  ctx.fillStyle = BG2D
  ctx.fillRect(0, 0, w, h)

  // Vignette
  const vg = ctx.createRadialGradient(w / 2, h / 2, Math.min(w, h) * 0.25, w / 2, h / 2, Math.max(w, h) * 0.78)
  vg.addColorStop(0, 'rgba(0,0,0,0)')
  vg.addColorStop(1, 'rgba(0,0,0,0.52)')
  ctx.fillStyle = vg; ctx.fillRect(0, 0, w, h)

  // Grid
  const step = 20 * sc
  if (step > 5) {
    const ox = ((w / 2 + tx) % step + step) % step
    const oy = ((h / 2 + ty) % step + step) % step
    ctx.strokeStyle = 'rgba(22,38,72,0.55)'; ctx.lineWidth = 0.5
    ctx.beginPath()
    for (let x = ox; x < w + step; x += step) { ctx.moveTo(x, 0); ctx.lineTo(x, h) }
    for (let y = oy; y < h + step; y += step) { ctx.moveTo(0, y); ctx.lineTo(w, y) }
    ctx.stroke()
  }
  // Major grid
  const mstep = 100 * sc
  if (mstep > 10 && mstep < w * 3) {
    const ox = ((w / 2 + tx) % mstep + mstep) % mstep
    const oy = ((h / 2 + ty) % mstep + mstep) % mstep
    ctx.strokeStyle = 'rgba(30,55,100,0.3)'; ctx.lineWidth = 0.8
    ctx.beginPath()
    for (let x = ox; x < w + mstep; x += mstep) { ctx.moveTo(x, 0); ctx.lineTo(x, h) }
    for (let y = oy; y < h + mstep; y += mstep) { ctx.moveTo(0, y); ctx.lineTo(w, y) }
    ctx.stroke()
  }

  let focusC = null

  // Soft halo only for meaningful large clusters; keeps dense maps from becoming a mesh.
  for (const c of cls) {
    const r = Math.min((c._size || 0.5) * sc, 28)
    if (r < 7.5 || (c._sizeRatio || 0) < 0.86) continue
    const isSel = selId !== null && selId === String(c.id)
    const isHov = hovId !== null && hovId === String(c.id)
    if (isSel || isHov) continue
    const [px, py] = w2c(c._pos[0], c._pos[1], w, h, tx, ty, sc)
    if (px + r * 3 < 0 || px - r * 3 > w || py + r * 3 < 0 || py - r * 3 > h) continue
    ctx.beginPath()
    ctx.arc(px, py, r * 1.65, 0, Math.PI * 2)
    ctx.fillStyle = c._color || '#6366f1'
    ctx.globalAlpha = 0.010
    ctx.fill()
    ctx.beginPath()
    ctx.arc(px, py, r * 1.18, 0, Math.PI * 2)
    ctx.globalAlpha = 0.018
    ctx.fill()
  }
  ctx.globalAlpha = 1

  // Main clusters
  for (const c of cls) {
    const isSel = selId !== null && selId === String(c.id)
    const isHov = hovId !== null && hovId === String(c.id)
    if (isSel || isHov) { focusC = c; continue }
    const rawR = Math.max((c._size || 0.5) * sc, 0.75)
    const r = Math.min(rawR, cls.length > 1200 ? 7.5 : 11)
    const [px, py] = w2c(c._pos[0], c._pos[1], w, h, tx, ty, sc)
    if (px + r < 0 || px - r > w || py + r < 0 || py - r > h) continue
    ctx.beginPath()
    ctx.arc(px, py, r, 0, Math.PI * 2)
    ctx.fillStyle = c._color || '#6366f1'
    ctx.globalAlpha = cls.length > 2500 ? 0.58 : cls.length > 1200 ? 0.68 : 0.82
    ctx.fill()
    if (c.production_hit_count) {
      ctx.globalAlpha = 0.9
      ctx.beginPath()
      ctx.arc(px, py, r + 3, 0, Math.PI * 2)
      ctx.strokeStyle = '#10b981'
      ctx.lineWidth = 1.4
      ctx.setLineDash([3, 3])
      ctx.stroke()
      ctx.setLineDash([])
    }
  }
  ctx.globalAlpha = 1

  // Neighbor lines for focus
  if (focusC) {
    const [fx, fy] = focusC._pos
    const sorted = [...cls].filter(c => String(c.id) !== String(focusC.id))
      .map(c => { const dx = c._pos[0] - fx, dy = c._pos[1] - fy; return { c, d2: dx * dx + dy * dy } })
      .sort((a, b) => a.d2 - b.d2).slice(0, 6)
    const [fpx, fpy] = w2c(fx, fy, w, h, tx, ty, sc)
    ctx.strokeStyle = 'rgba(56,75,120,0.55)'; ctx.lineWidth = 0.8; ctx.setLineDash([3, 4])
    for (const { c } of sorted) {
      const [nx, ny] = w2c(c._pos[0], c._pos[1], w, h, tx, ty, sc)
      ctx.beginPath(); ctx.moveTo(fpx, fpy); ctx.lineTo(nx, ny); ctx.stroke()
    }
    ctx.setLineDash([])
  }

  // Focus cluster (selected / hovered) on top
  if (focusC) {
    const isSel = selId !== null && selId === String(focusC.id)
    const rawFocusR = Math.max((focusC._size || 0.5) * sc * (isSel ? 1.7 : 1.35), 1.5)
    const r = Math.min(rawFocusR, isSel ? 16 : 13)
    const [px, py] = w2c(focusC._pos[0], focusC._pos[1], w, h, tx, ty, sc)

    // Glow
    ctx.beginPath(); ctx.arc(px, py, r * 3, 0, Math.PI * 2)
    ctx.fillStyle = focusC._color || '#6366f1'; ctx.globalAlpha = 0.07; ctx.fill()
    ctx.beginPath(); ctx.arc(px, py, r * 1.8, 0, Math.PI * 2); ctx.globalAlpha = 0.12; ctx.fill()
    ctx.globalAlpha = 1

    if (isSel) {
      ctx.beginPath(); ctx.arc(px, py, r + 5, 0, Math.PI * 2)
      ctx.strokeStyle = focusC._color || '#6366f1'; ctx.lineWidth = 1.5; ctx.globalAlpha = 0.55; ctx.stroke(); ctx.globalAlpha = 1
    }
    ctx.beginPath(); ctx.arc(px, py, r, 0, Math.PI * 2)
    ctx.fillStyle = isSel ? '#f8fafc' : '#cbd5e1'; ctx.fill()

    if (focusC.production_hit_count) {
      ctx.beginPath(); ctx.arc(px, py, r + 8, 0, Math.PI * 2)
      ctx.strokeStyle = '#10b981'; ctx.lineWidth = 2; ctx.setLineDash([4, 4]); ctx.stroke(); ctx.setLineDash([])
      ctx.font = '700 9px Inter,system-ui,sans-serif'
      ctx.fillStyle = '#10b981'
      ctx.fillText(`${Number(focusC.production_hit_count).toLocaleString()} prod`, px + r + 8, py - r - 3)
    }

    if (isSel) {
      const { dx, dy, sim } = selectedMedoidOffset(focusC, 1)
      const mx = px + dx
      const my = py + dy
      ctx.setLineDash([4, 5])
      ctx.strokeStyle = 'rgba(248,250,252,0.55)'
      ctx.lineWidth = 1
      ctx.beginPath(); ctx.moveTo(px, py); ctx.lineTo(mx, my); ctx.stroke()
      ctx.setLineDash([])

      ctx.beginPath(); ctx.arc(px, py, 7, 0, Math.PI * 2)
      ctx.strokeStyle = '#3b82f6'; ctx.lineWidth = 2; ctx.stroke()
      ctx.beginPath(); ctx.arc(px, py, 2.2, 0, Math.PI * 2)
      ctx.fillStyle = '#3b82f6'; ctx.fill()

      drawDiamond(ctx, mx, my, 6)
      ctx.fillStyle = '#f472b6'; ctx.fill()
      ctx.strokeStyle = '#f8fafc'; ctx.lineWidth = 1.5; ctx.stroke()

      const metric = `medoid sim ${sim.toFixed(2)}`
      ctx.font = '600 9px Inter,system-ui,sans-serif'
      const mw = ctx.measureText(metric).width
      roundRect(ctx, (px + mx) / 2 - mw / 2 - 5, (py + my) / 2 - 18, mw + 10, 14, 4)
      ctx.fillStyle = 'rgba(3,6,16,0.78)'; ctx.fill()
      ctx.fillStyle = '#cbd5e1'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle'
      ctx.fillText(metric, (px + mx) / 2, (py + my) / 2 - 11)
      ctx.textAlign = 'left'; ctx.textBaseline = 'alphabetic'
    }
  }

  // Labels
  if (showL || sc > 9) {
    // When the label toggle is ON, do not cap labels. Draw every visible
    // filtered cluster label. Auto-label mode is still gated so the map stays
    // readable when labels are OFF.
    const labCls = showL
      ? cls.filter(c => String(c.id) !== selId && String(c.id) !== hovId)
      : cls
          .filter(c => {
            const ratio = c._sizeRatio || 0
            const threshold = sc > 24 ? 0.28 : sc > 14 ? 0.48 : 0.70
            return ratio >= threshold && String(c.id) !== selId && String(c.id) !== hovId
          })
          .sort((a, b) => (b._sizeRatio || 0) - (a._sizeRatio || 0))
          .slice(0, sc > 24 ? 34 : sc > 14 ? 18 : 8)

    ctx.textAlign = 'center'; ctx.textBaseline = 'top'
    ctx.font = showL && cls.length > 1200 ? '7.5px Inter,system-ui,sans-serif' : '9px Inter,system-ui,sans-serif'
    for (const c of labCls) {
      const name = c.display_name || c.medoid_label || c.cluster_id
      if (!name) continue
      const [px, py] = w2c(c._pos[0], c._pos[1], w, h, tx, ty, sc)
      if (px < -150 || px > w + 150 || py < -30 || py > h + 30) continue
      const r = Math.max((c._size || 0.5) * sc, 1)
      const lbl = shortLabel(name, showL && cls.length > 1200 ? 22 : 26)
      const tw = ctx.measureText(lbl).width
      roundRect(ctx, px - tw / 2 - 4, py + r + 3, tw + 8, 13, 3)
      ctx.fillStyle = showL ? 'rgba(6,10,20,0.64)' : 'rgba(6,10,20,0.72)'
      ctx.fill()
      ctx.fillStyle = showL ? 'rgba(226,232,240,0.82)' : 'rgba(148,163,184,0.9)'
      ctx.fillText(lbl, px, py + r + 5)
    }
    ctx.textAlign = 'left'; ctx.textBaseline = 'alphabetic'
  }

  if (focusC) {
    const [px, py] = w2c(focusC._pos[0], focusC._pos[1], w, h, tx, ty, sc)
    drawTooltip(ctx, focusC, px, py, w, h)
  }
}

// ── 3D draw ────────────────────────────────────────────────────────────────────
function project3D(x, y, z, rotX, rotY, fov, zoom) {
  // Rotate Y
  const x1 = x * Math.cos(rotY) + z * Math.sin(rotY)
  const z1 = -x * Math.sin(rotY) + z * Math.cos(rotY)
  // Rotate X
  const y2 = y * Math.cos(rotX) - z1 * Math.sin(rotX)
  const z2 = y * Math.sin(rotX) + z1 * Math.cos(rotX)
  // Perspective
  const s = fov / Math.max(fov + z2, 8)
  return { sx: x1 * s * zoom, sy: -y2 * s * zoom, z: z2, s: s * zoom }
}

function draw3D(ctx, cls, w, h, rotX, rotY, fov, zoom, panX, panY, selId, hovId, showL) {
  ctx.fillStyle = BG3D; ctx.fillRect(0, 0, w, h)

  // Stars
  ctx.fillStyle = '#ffffff'
  for (let i = 0; i < 320; i++) {
    const sx = seededRand(i * 17 + 3) * w
    const sy = seededRand(i * 31 + 7) * h
    const r  = 0.4 + seededRand(i * 43 + 11) * 0.9
    ctx.globalAlpha = 0.15 + seededRand(i * 53 + 13) * 0.45
    ctx.beginPath(); ctx.arc(sx, sy, r, 0, Math.PI * 2); ctx.fill()
  }
  ctx.globalAlpha = 1

  // Project all clusters
  const items = cls.map(c => {
    const [ox, oy, oz] = c._pos
    const { sx, sy, z, s } = project3D(ox, oy, oz, rotX, rotY, fov, zoom)
    const isSel = selId !== null && selId === String(c.id)
    const isHov = hovId !== null && hovId === String(c.id)
    const rawBaseR = Math.max((c._size || 0.5) * s * 0.9, 0.5)
    const baseR = Math.min(rawBaseR, cls.length > 1200 ? 7 : 10)
    const r = baseR * (isSel ? 1.6 : isHov ? 1.25 : 1.0)
    return { c, px: w / 2 + panX + sx, py: h / 2 + panY + sy, r, z, isSel, isHov }
  }).sort((a, b) => b.z - a.z)  // painter's: far first

  const maxZ = 80, minZ = -80
  let focusItem = null

  for (const item of items) {
    const { c, px, py, r, z, isSel, isHov } = item
    if (isSel || isHov) { focusItem = item; continue }
    if (px + r < 0 || px - r > w || py + r < 0 || py - r > h) continue
    if (r < 0.3) continue

    const depth = (z - minZ) / (maxZ - minZ)
    const alpha = 0.25 + Math.max(0, Math.min(1, depth)) * 0.65

    // Glow for larger clusters
    if (r > 3) {
      ctx.beginPath(); ctx.arc(px, py, r * 1.8, 0, Math.PI * 2)
      ctx.fillStyle = c._color || '#6366f1'; ctx.globalAlpha = alpha * 0.035; ctx.fill()
    }
    ctx.beginPath(); ctx.arc(px, py, Math.max(r, 0.5), 0, Math.PI * 2)
    ctx.fillStyle = c._color || '#6366f1'; ctx.globalAlpha = alpha * 0.9; ctx.fill()
    if (c.production_hit_count) {
      ctx.globalAlpha = Math.min(1, alpha + 0.15)
      ctx.beginPath(); ctx.arc(px, py, Math.max(r + 3, 4), 0, Math.PI * 2)
      ctx.strokeStyle = '#10b981'; ctx.lineWidth = 1.3; ctx.setLineDash([3, 3]); ctx.stroke(); ctx.setLineDash([])
    }
  }
  ctx.globalAlpha = 1



  // Labels in 3D mode. When the label toggle is ON, do not cap labels.
  // Draw every visible filtered cluster label after projection. Auto-label mode
  // remains capped when the toggle is OFF.
  if (showL || zoom > 7.5) {
    const labelItems = showL
      ? items.filter(item => item.r > 0.35)
      : items
          .filter(item => item.r > 1.1 && (item.c._sizeRatio || 0) >= 0.55)
          .sort((a, b) => (b.c._sizeRatio || 0) - (a.c._sizeRatio || 0))
          .slice(0, 28)

    ctx.textAlign = 'center'
    ctx.textBaseline = 'top'
    ctx.font = showL && cls.length > 1200 ? '7.5px Inter,system-ui,sans-serif' : '9px Inter,system-ui,sans-serif'
    for (const item of labelItems) {
      const { c, px, py, r } = item
      if (px < -150 || px > w + 150 || py < -34 || py > h + 34) continue
      const name = c.display_name || c.medoid_label || c.cluster_id
      if (!name) continue
      const lbl = shortLabel(name, showL && cls.length > 1200 ? 22 : 28)
      const tw = ctx.measureText(lbl).width
      roundRect(ctx, px - tw / 2 - 5, py + r + 4, tw + 10, 14, 4)
      ctx.fillStyle = showL ? 'rgba(3,6,16,0.68)' : 'rgba(3,6,16,0.82)'
      ctx.fill()
      ctx.strokeStyle = 'rgba(255,255,255,0.06)'
      ctx.lineWidth = 1
      ctx.stroke()
      ctx.fillStyle = showL ? 'rgba(226,232,240,0.82)' : 'rgba(226,232,240,0.90)'
      ctx.fillText(lbl, px, py + r + 6)
    }
    ctx.textAlign = 'left'
    ctx.textBaseline = 'alphabetic'
  }

  // Focus on top
  if (focusItem) {
    const { c, px, py, r, isSel } = focusItem
    ctx.beginPath(); ctx.arc(px, py, r * 3, 0, Math.PI * 2)
    ctx.fillStyle = c._color || '#6366f1'; ctx.globalAlpha = 0.1; ctx.fill()
    ctx.globalAlpha = 1
    if (isSel) {
      ctx.beginPath(); ctx.arc(px, py, r + 5, 0, Math.PI * 2)
      ctx.strokeStyle = c._color || '#6366f1'; ctx.lineWidth = 1.5; ctx.globalAlpha = 0.6; ctx.stroke(); ctx.globalAlpha = 1
    }
    ctx.beginPath(); ctx.arc(px, py, r, 0, Math.PI * 2)
    ctx.fillStyle = isSel ? '#f8fafc' : '#cbd5e1'; ctx.fill()

    if (c.production_hit_count) {
      ctx.beginPath(); ctx.arc(px, py, r + 8, 0, Math.PI * 2)
      ctx.strokeStyle = '#10b981'; ctx.lineWidth = 2; ctx.setLineDash([4, 4]); ctx.stroke(); ctx.setLineDash([])
      ctx.font = '700 9px Inter,system-ui,sans-serif'
      ctx.fillStyle = '#10b981'
      ctx.fillText(`${Number(c.production_hit_count).toLocaleString()} prod`, px + r + 8, py - r - 3)
    }

    if (isSel) {
      const { dx, dy, sim } = selectedMedoidOffset(c, 0.82)
      const mx = px + dx
      const my = py + dy
      ctx.setLineDash([4, 5])
      ctx.strokeStyle = 'rgba(248,250,252,0.55)'
      ctx.lineWidth = 1
      ctx.beginPath(); ctx.moveTo(px, py); ctx.lineTo(mx, my); ctx.stroke()
      ctx.setLineDash([])
      ctx.beginPath(); ctx.arc(px, py, 7, 0, Math.PI * 2)
      ctx.strokeStyle = '#3b82f6'; ctx.lineWidth = 2; ctx.stroke()
      ctx.beginPath(); ctx.arc(px, py, 2.2, 0, Math.PI * 2)
      ctx.fillStyle = '#3b82f6'; ctx.fill()
      drawDiamond(ctx, mx, my, 6)
      ctx.fillStyle = '#f472b6'; ctx.fill()
      ctx.strokeStyle = '#f8fafc'; ctx.lineWidth = 1.5; ctx.stroke()
      ctx.font = '600 9px Inter,system-ui,sans-serif'
      ctx.fillStyle = '#cbd5e1'
      ctx.fillText(`sim ${sim.toFixed(2)}`, mx + 9, my + 3)
    }

    if (r > 2) drawTooltip(ctx, c, px, py, w, h)
  }
}

function nearest3D(cls, mx, my, w, h, rotX, rotY, fov, zoom, panX, panY) {
  let best = null
  let bestScore = Infinity
  for (const c of cls) {
    const [ox, oy, oz] = c._pos
    const { sx, sy, z, s } = project3D(ox, oy, oz, rotX, rotY, fov, zoom)
    const px = w / 2 + panX + sx
    const py = h / 2 + panY + sy
    const r = Math.max((c._size || 0.5) * s * 0.9, 3)
    const dx = px - mx
    const dy = py - my
    const d2 = dx * dx + dy * dy
    const hitRadius = Math.max(10, r + 8)
    if (d2 <= hitRadius * hitRadius && d2 < bestScore) {
      bestScore = d2 + z * 0.002
      best = c
    }
  }
  return best
}

// ── Component ──────────────────────────────────────────────────────────────────
export default function SemanticScene({ clusters, colorMode, viewMode, showLabels }) {
  const {
    selectedClusterId, hoveredClusterId,
    setHoveredClusterId, setSelectedClusterId,
    cameraReset,
  } = useStore()

  const is3d = viewMode === '3d'
  const layoutMode = is3d ? 'galaxy' : 'map'

  const layoutKey = useMemo(() => {
    const list = clusters || []
    if (!list.length) return 'empty'
    const first = list[0]?.id || list[0]?.cluster_id || ''
    const last = list[list.length - 1]?.id || list[list.length - 1]?.cluster_id || ''
    const anomalyCount = list.reduce((n, c) => n + (c.is_true_anomaly_cluster ? 1 : 0), 0)
    return `${viewMode}|${colorMode}|${list.length}|${anomalyCount}|${first}|${last}`
  }, [clusters, viewMode, colorMode])

  const positioned = useMemo(
    () => clusters?.length ? buildSpatialLayout(clusters, { colorMode, viewMode: layoutMode }) : [],
    [clusters, colorMode, layoutMode]
  )

  const canvasRef  = useRef(null)
  const xf2d       = useRef({ tx: 0, ty: 0, sc: 5, w: 0, h: 0 })
  const xf3d       = useRef({ rotX: 0.28, rotY: -0.45, fov: 240, zoom: 4.2, panX: 0, panY: 0, w: 0, h: 0 })
  const dragRef    = useRef({ on: false, sx: 0, sy: 0, stx: 0, sty: 0, spx: 0, spy: 0, srot: [0, 0], mode: 'pan', moved: false })
  const rafRef     = useRef(null)
  const dirtyRef   = useRef(true)
  const fitted2d   = useRef(false)
  const lastFitKey = useRef('')

  const posRef   = useRef(positioned); posRef.current   = positioned
  const selRef   = useRef(selectedClusterId); selRef.current = selectedClusterId
  const hovRef   = useRef(hoveredClusterId);  hovRef.current = hoveredClusterId
  const labRef   = useRef(showLabels);        labRef.current = showLabels
  const is3dRef  = useRef(is3d);             is3dRef.current = is3d

  // Auto-fit only when the actual dataset/view/filter signature changes.
  // This prevents manual zoom from snapping back during normal re-renders.
  useEffect(() => {
    dirtyRef.current = true
    if (lastFitKey.current === layoutKey) return
    lastFitKey.current = layoutKey

    if (is3d || !positioned.length) return
    const { w, h } = xf2d.current
    if (!w || !h) { fitted2d.current = false; return }
    const fit = fitAll(positioned, w, h)
    xf2d.current = { ...xf2d.current, ...fit }
    fitted2d.current = true
    dirtyRef.current = true
  }, [positioned, is3d, layoutKey])

  // Camera reset
  useEffect(() => {
    const { w, h } = is3d ? xf3d.current : xf2d.current
    if (!w || !h) return
    if (is3d) {
      xf3d.current = { rotX: 0.28, rotY: -0.45, fov: 240, zoom: 4.2, panX: 0, panY: 0, w, h }
    } else {
      if (posRef.current.length) {
        const fit = fitAll(posRef.current, w, h)
        xf2d.current = { ...xf2d.current, ...fit }
      }
    }
    dirtyRef.current = true
  }, [cameraReset, is3d])

  // Direct scene commands from the overlay controls. Keeps the toolbar buttons
  // functional without coupling the canvas to the React overlay.
  useEffect(() => {
    function resetCurrentView() {
      const { w, h } = is3dRef.current ? xf3d.current : xf2d.current
      if (!w || !h) return
      if (is3dRef.current) {
        xf3d.current = { rotX: 0.28, rotY: -0.45, fov: 240, zoom: 4.2, panX: 0, panY: 0, w, h }
      } else if (posRef.current.length) {
        xf2d.current = { ...xf2d.current, ...fitAll(posRef.current, w, h) }
      }
      dirtyRef.current = true
    }

    function zoomCurrentView(direction) {
      const factor = direction === 'in' ? 1.22 : 1 / 1.22
      if (is3dRef.current) {
        const nextZoom = clamp(xf3d.current.zoom * factor, 0.65, 38)
        xf3d.current = { ...xf3d.current, zoom: nextZoom }
      } else {
        const nextScale = clamp(xf2d.current.sc * factor, 0.25, 260)
        xf2d.current = { ...xf2d.current, sc: nextScale }
      }
      dirtyRef.current = true
    }

    function onSceneCommand(event) {
      const action = event?.detail?.action
      if (action === 'reset') resetCurrentView()
      if (action === 'zoomIn') zoomCurrentView('in')
      if (action === 'zoomOut') zoomCurrentView('out')
      if (action === 'fullscreen') {
        const host = canvasRef.current?.parentElement
        if (host?.requestFullscreen && !document.fullscreenElement) host.requestFullscreen().catch(() => {})
        else if (document.exitFullscreen && document.fullscreenElement) document.exitFullscreen().catch(() => {})
      }
    }

    window.addEventListener('semantic-scene-command', onSceneCommand)
    return () => window.removeEventListener('semantic-scene-command', onSceneCommand)
  }, [])

  // Mark dirty on reactive changes
  useEffect(() => { dirtyRef.current = true }, [selectedClusterId, hoveredClusterId, showLabels, is3d])

  const draw = useCallback(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const dpr = window.devicePixelRatio || 1
    const ctx = canvas.getContext('2d')

    if (is3dRef.current) {
      const { rotX, rotY, fov, zoom, panX, panY, w, h } = xf3d.current
      if (!w || !h) return
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      const selId = selRef.current != null ? String(selRef.current) : null
      const hovId = hovRef.current != null ? String(hovRef.current) : null
      draw3D(ctx, posRef.current, w, h, rotX, rotY, fov, zoom, panX, panY, selId, hovId, labRef.current)
    } else {
      const { tx, ty, sc, w, h } = xf2d.current
      if (!w || !h) return
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      const selId = selRef.current != null ? String(selRef.current) : null
      const hovId = hovRef.current != null ? String(hovRef.current) : null
      draw2D(ctx, posRef.current, w, h, tx, ty, sc, selId, hovId, labRef.current)
    }

    dirtyRef.current = false
  }, [])

  // RAF loop
  useEffect(() => {
    let alive = true
    function tick() {
      if (!alive) return
      if (dirtyRef.current) draw()
      rafRef.current = requestAnimationFrame(tick)
    }
    tick()
    return () => { alive = false; cancelAnimationFrame(rafRef.current) }
  }, [draw])

  // Resize observer
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const parent = canvas.parentElement
    if (!parent) return

    function resize() {
      const { width, height } = parent.getBoundingClientRect()
      if (!width || !height) return
      const dpr = window.devicePixelRatio || 1
      canvas.width  = Math.round(width  * dpr)
      canvas.height = Math.round(height * dpr)
      canvas.style.width  = width  + 'px'
      canvas.style.height = height + 'px'

      const cls = posRef.current
      if (!fitted2d.current && cls.length && !is3dRef.current) {
        const fit = fitAll(cls, width, height)
        xf2d.current = { ...fit, w: width, h: height }
        fitted2d.current = true
      } else {
        xf2d.current = { ...xf2d.current, w: width, h: height }
      }
      xf3d.current = { ...xf3d.current, w: width, h: height }
      dirtyRef.current = true
    }

    const ro = new ResizeObserver(resize)
    ro.observe(parent)
    resize()
    return () => ro.disconnect()
  }, [])

  // Wheel zoom (passive: false)
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    function onWheel(e) {
      e.preventDefault()
      const rect = canvas.getBoundingClientRect()
      const mx = e.clientX - rect.left
      const my = e.clientY - rect.top
      const delta = Math.max(-80, Math.min(80, e.deltaY || 0))
      const factor = Math.exp(-delta * 0.0028)

      if (is3dRef.current) {
        const { zoom, panX, panY, w, h } = xf3d.current
        const hasHorizontalGesture = Math.abs(e.deltaX || 0) > Math.abs(e.deltaY || 0) * 0.65
        const shouldPan = e.shiftKey || hasHorizontalGesture
        if (shouldPan) {
          xf3d.current = {
            ...xf3d.current,
            panX: panX - (e.deltaX || 0) * 1.2,
            panY: panY - (e.deltaY || 0) * 1.2,
          }
        } else {
          const newZoom = Math.max(0.65, Math.min(38, zoom * factor))
          const ratio = newZoom / zoom
          xf3d.current = {
            ...xf3d.current,
            zoom: newZoom,
            panX: (mx - w / 2) * (1 - ratio) + panX * ratio,
            panY: (my - h / 2) * (1 - ratio) + panY * ratio,
          }
        }
      } else {
        const { tx, ty, sc, w, h } = xf2d.current
        const newSc = Math.max(0.25, Math.min(260, sc * factor))
        const ratio = newSc / sc
        xf2d.current = {
          ...xf2d.current,
          sc: newSc,
          tx: (mx - w / 2) * (1 - ratio) + tx * ratio,
          ty: (my - h / 2) * (1 - ratio) + ty * ratio,
        }
      }
      dirtyRef.current = true
    }
    canvas.addEventListener('wheel', onWheel, { passive: false })
    return () => canvas.removeEventListener('wheel', onWheel)
  }, [])

  // ── Mouse ────────────────────────────────────────────────────────────────────
  function onMouseDown(e) {
    const { rotX, rotY, panX, panY } = xf3d.current
    const mode = is3dRef.current && !(e.shiftKey || e.ctrlKey || e.altKey || e.metaKey || e.button === 1 || e.button === 2) ? 'rotate' : 'pan'
    dragRef.current = {
      on: true, sx: e.clientX, sy: e.clientY,
      stx: xf2d.current.tx, sty: xf2d.current.ty,
      spx: panX, spy: panY,
      srot: [rotX, rotY], mode, moved: false,
    }
    e.currentTarget.style.cursor = mode === 'rotate' ? 'grabbing' : 'move'
  }

  function onMouseMove(e) {
    if (dragRef.current.on) {
      const dx = e.clientX - dragRef.current.sx
      const dy = e.clientY - dragRef.current.sy
      if (Math.abs(dx) > 2 || Math.abs(dy) > 2) dragRef.current.moved = true
      if (is3dRef.current) {
        if (dragRef.current.mode === 'rotate') {
          const [srX, srY] = dragRef.current.srot
          xf3d.current = {
            ...xf3d.current,
            rotY: srY + dx * 0.006,
            rotX: Math.max(-Math.PI / 2.2, Math.min(Math.PI / 2.2, srX + dy * 0.006)),
          }
        } else {
          xf3d.current = { ...xf3d.current, panX: dragRef.current.spx + dx * 1.35, panY: dragRef.current.spy + dy * 1.35 }
        }
      } else {
        xf2d.current = { ...xf2d.current, tx: dragRef.current.stx + dx, ty: dragRef.current.sty + dy }
      }
      dirtyRef.current = true
      return
    }
    const canvas = canvasRef.current
    if (!canvas) return
    const rect = canvas.getBoundingClientRect()
    if (is3dRef.current) {
      const { rotX, rotY, fov, zoom, panX, panY, w, h } = xf3d.current
      const hit = nearest3D(posRef.current, e.clientX - rect.left, e.clientY - rect.top, w, h, rotX, rotY, fov, zoom, panX, panY)
      setHoveredClusterId(hit ? hit.id : null)
      return
    }
    const { tx, ty, sc, w, h } = xf2d.current
    const [wx, wy] = c2w(e.clientX - rect.left, e.clientY - rect.top, w, h, tx, ty, sc)
    const hit = nearest2D(posRef.current, wx, wy, 18 / sc)
    setHoveredClusterId(hit ? hit.id : null)
  }

  function onMouseUp(e) {
    const wasDrag = dragRef.current.moved
    dragRef.current = { ...dragRef.current, on: false, moved: false }
    e.currentTarget.style.cursor = is3dRef.current ? 'grab' : 'crosshair'
    if (!wasDrag) {
      const canvas = canvasRef.current
      if (!canvas) return
      const rect = canvas.getBoundingClientRect()
      if (is3dRef.current) {
        const { rotX, rotY, fov, zoom, panX, panY, w, h } = xf3d.current
        const hit = nearest3D(posRef.current, e.clientX - rect.left, e.clientY - rect.top, w, h, rotX, rotY, fov, zoom, panX, panY)
        if (hit) setSelectedClusterId(prev => String(prev) === String(hit.id) ? null : hit.id)
        else setSelectedClusterId(null)
      } else {
        const { tx, ty, sc, w, h } = xf2d.current
        const [wx, wy] = c2w(e.clientX - rect.left, e.clientY - rect.top, w, h, tx, ty, sc)
        const hit = nearest2D(posRef.current, wx, wy, 18 / sc)
        if (hit) setSelectedClusterId(prev => String(prev) === String(hit.id) ? null : hit.id)
        else      setSelectedClusterId(null)
      }
    }
  }

  function onMouseLeave(e) {
    dragRef.current = { ...dragRef.current, on: false }
    e.currentTarget.style.cursor = is3dRef.current ? 'grab' : 'crosshair'
    setHoveredClusterId(null)
  }

  return (
    <canvas
      ref={canvasRef}
      style={{
        display: 'block', width: '100%', height: '100%',
        cursor: is3d ? 'grab' : 'crosshair', userSelect: 'none',
      }}
      onContextMenu={(e) => e.preventDefault()}
      onMouseDown={onMouseDown}
      onMouseMove={onMouseMove}
      onMouseUp={onMouseUp}
      onMouseLeave={onMouseLeave}
    />
  )
}
