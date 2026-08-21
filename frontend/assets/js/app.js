/* ============================================================
   LimeAuto After-Sales Portal — 共享 JS
   前端骨架：mock 数据 + fetch 封装 + 工具函数
   说明：V1 前端骨架用 mock 数据占位；接入后端后，
   将 window.MOCK 替换为真实 AJAX（见下方 api()）。
   ============================================================ */
(function () {
  'use strict';

  /* ---------- 工具 ---------- */
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const esc = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

  /* ---------- Mock 数据（接入后端后删除） ---------- */
  const MOCK = {
    vehicles: [
      { id: 1024, vin: 'LGXCE4CC0N0123456', brand: 'BYD', model: 'Sealion 6 (海狮06)', plate: 'IR-88421', dealer: 'KTL', saleDate: '2026-03-15', warrantyStart: '2026-03-15', wholeMonths: 72, wholeKm: 150000, evMonths: 96, evKm: 240000, status: 'In Warranty', mileage: 18400 },
      { id: 1025, vin: 'LGXCE4CC0N0654321', brand: 'BYD', model: 'Sealion 7 (海狮07)', plate: 'IR-90337', dealer: 'KTL', saleDate: '2026-05-02', warrantyStart: '2026-05-02', wholeMonths: 72, wholeKm: 150000, evMonths: 96, evKm: 240000, status: 'In Warranty', mileage: 9200 },
      { id: 2033, vin: 'LFV3B2F55P7010203', brand: 'LEAP', model: 'Leapmotor C10', plate: 'UAE-55129', dealer: 'AvaMotor', saleDate: '2025-11-20', warrantyStart: '2025-11-20', wholeMonths: 60, wholeKm: 120000, evMonths: 96, evKm: 200000, status: 'In Warranty', mileage: 31000 }
    ],
    claims: [
      { no: 'CLM-2026-0007', vehicle: 'LGXCE4CC0N0123456', model: 'Sealion 6', fault: 'Battery pack failure, cannot charge', status: 'Under Review', date: '2026-08-17', parts: [{ oe: 'BYD-BAT-8842', qty: 1 }] },
      { no: 'CLM-2026-0006', vehicle: 'LGXCE4CC0N0654321', model: 'Sealion 7', fault: 'Rear tail light crack (transport)', status: 'Approved', date: '2026-08-14', parts: [{ oe: 'BYD-LMP-2207', qty: 1 }] },
      { no: 'CLM-2026-0005', vehicle: 'LFV3B2F55P7010203', model: 'C10', fault: 'Brake pad wear (normal)', status: 'Rejected', date: '2026-08-10', parts: [{ oe: 'LEAP-BRK-1011', qty: 4 }] },
      { no: 'CLM-2026-0004', vehicle: 'LGXCE4CC0N0123456', model: 'Sealion 6', fault: 'Infotainment screen flicker', status: 'Shipped', date: '2026-08-05', parts: [{ oe: 'BYD-SCR-3320', qty: 1 }] }
    ],
    oeParts: [
      { oe: 'BYD-BAT-8842', name: 'Battery Pack Assembly (LFP, 71.8kWh)', cat: 'Powertrain / Battery', models: ['Sealion 6'], warranty: 'EV Core (96mo)', wear: false, thumb: '🔋', brand: 'BYD' },
      { oe: 'BYD-BAT-8850', name: 'Battery Pack Assembly (LFP, 91kWh)', cat: 'Powertrain / Battery', models: ['Sealion 7'], warranty: 'EV Core (96mo)', wear: false, thumb: '🔋', brand: 'BYD' },
      { oe: 'BYD-BMS-7710', name: 'BMS Controller Module', cat: 'Powertrain / Battery', models: ['Sealion 6', 'Sealion 7'], warranty: 'EV Core (96mo)', wear: false, thumb: '🧩', brand: 'BYD' },
      { oe: 'BYD-MTR-5501', name: 'Front Drive Motor', cat: 'Powertrain / E-Drive', models: ['Sealion 6'], warranty: 'EV Core (96mo)', wear: false, thumb: '⚙️', brand: 'BYD' },
      { oe: 'BYD-LMP-2207', name: 'Rear Tail Light (LED)', cat: 'Body / Lighting', models: ['Sealion 7'], warranty: 'General (72mo)', wear: false, thumb: '💡', brand: 'BYD' },
      { oe: 'BYD-SCR-3320', name: 'Center Infotainment Screen', cat: 'Electrics / Infotainment', models: ['Sealion 6', 'Sealion 7'], warranty: 'General (72mo)', wear: false, thumb: '🖥️', brand: 'BYD' },
      { oe: 'LEAP-BRK-1011', name: 'Front Brake Pad Set', cat: 'Chassis / Brake', models: ['C10'], warranty: 'Wear Item', wear: true, thumb: '🛞', brand: 'LEAP' },
      { oe: 'BYD-WPR-1180', name: 'Wiper Blade Set', cat: 'Wear Items', models: ['Sealion 6'], warranty: 'Wear Item', wear: true, thumb: '🌧️', brand: 'BYD' }
    ],
    inventory: [
      { sku: 'SKU-3001', oe: 'BYD-BAT-8842', name: 'Battery Pack (71.8kWh)', qty: 3, location: 'WH-A-01', safety: 2 },
      { sku: 'SKU-3002', oe: 'BYD-LMP-2207', name: 'Rear Tail Light (LED)', qty: 12, location: 'WH-B-03', safety: 5 },
      { sku: 'SKU-3003', oe: 'BYD-SCR-3320', name: 'Infotainment Screen', qty: 1, location: 'WH-A-05', safety: 3 },
      { sku: 'SKU-3004', oe: 'LEAP-BRK-1011', name: 'Brake Pad Set', qty: 40, location: 'WH-C-01', safety: 10 }
    ],
    shipments: [
      { no: 'SHP-2026-0102', claim: 'CLM-2026-0004', part: 'Infotainment Screen', qty: 1, carrier: 'DHL', tracking: 'DHL-8842100', status: 'In Transit', eta: '2026-08-24' }
    ]
  };

  /* ---------- 状态徽章映射 ---------- */
  const STATUS_BADGE = {
    'Draft': 'b-gray', 'Submitted': 'b-blue', 'Under Review': 'b-amber',
    'Approved': 'b-green', 'Partial': 'b-teal', 'Rejected': 'b-red',
    'Shipped': 'b-indigo', 'Closed': 'b-gray',
    'In Transit': 'b-blue', 'Delivered': 'b-green', 'Pending': 'b-amber'
  };
  const badge = (status) => {
    const cls = STATUS_BADGE[status] || 'b-gray';
    return `<span class="badge ${cls}"><span class="dot"></span>${esc(status)}</span>`;
  };

  /* ---------- fetch 封装 ----------
     骨架阶段：优先走 mock，未命中再尝试真实接口。
     接入后端：把 USE_MOCK 设为 false，api() 直接 fetch /api/*。 */
  const USE_MOCK = true;
  const TOKEN_KEY = 'limeauto_token';
  const api = async (method, url, body) => {
    if (USE_MOCK) {
      return new Promise((resolve) => setTimeout(() => {
        if (url.includes('/login')) resolve({ success: true, data: { token: 'mock-token', role: body.role } });
        if (url.includes('/vehicles')) resolve({ success: true, data: MOCK.vehicles });
        if (url.includes('/claims')) resolve({ success: true, data: MOCK.claims });
        if (url.includes('/oe-parts')) resolve({ success: true, data: MOCK.oeParts });
        if (url.includes('/inventory')) resolve({ success: true, data: MOCK.inventory });
        if (url.includes('/shipments')) resolve({ success: true, data: MOCK.shipments });
        resolve({ success: true, data: null });
      }, 180));
    }
    const res = await fetch(url, {
      method, headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + localStorage.getItem(TOKEN_KEY) },
      body: body ? JSON.stringify(body) : undefined
    });
    return res.json();
  };

  /* ---------- 登录 / 会话 ---------- */
  const login = (role) => {
    localStorage.setItem(TOKEN_KEY, 'mock-token');
    localStorage.setItem('limeauto_role', role);
    location.href = role === 'admin' ? 'admin/dashboard.html' : 'dealer/dashboard.html';
  };
  const role = () => localStorage.getItem('limeauto_role') || 'dealer';
  const logout = () => { localStorage.clear(); location.href = '../index.html'; };

  /* ---------- 暴露 ---------- */
  window.LIME = { $, $$, esc, MOCK, api, badge, login, role, logout, USE_MOCK };

  /* ---------- 侧边栏高亮（页面加载后调用） ---------- */
  document.addEventListener('DOMContentLoaded', () => {
    const path = location.pathname.split('/').pop();
    $$('.sidebar nav a').forEach((a) => {
      if (a.getAttribute('href') === path) a.classList.add('active');
    });
    // 顶部用户角色显示
    const r = role();
    const uname = $('#topbar-user');
    if (uname) uname.textContent = r === 'admin' ? 'Alan (Admin)' : 'KTL Dealer';
  });
})();
