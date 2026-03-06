from __future__ import annotations

import html
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse


APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "data" / "home_menu.sqlite3"
STATIC_DIR = APP_DIR / "static"

HOST = os.environ.get("HOME_MENU_HOST", "127.0.0.1")
PORT = int(os.environ.get("HOME_MENU_PORT", "8010"))


Category = dict[str, str]


def get_categories() -> list[Category]:
    # 常见家用分类 + 彩色图标（用 emoji + 颜色）
    return [
        {"id": "home", "name": "家常菜", "emoji": "🍛", "color": "#ff9f7a"},
        {"id": "soup", "name": "汤羹", "emoji": "🍲", "color": "#f97373"},
        {"id": "salad", "name": "沙拉", "emoji": "🥗", "color": "#34d399"},
        {"id": "noodle", "name": "面食", "emoji": "🍜", "color": "#60a5fa"},
        {"id": "rice", "name": "主食", "emoji": "🍚", "color": "#facc15"},
        {"id": "breakfast", "name": "早餐", "emoji": "🥐", "color": "#fb923c"},
        {"id": "dessert", "name": "甜品", "emoji": "🍰", "color": "#fb6fbb"},
        {"id": "drink", "name": "饮品", "emoji": "🧋", "color": "#a855f7"},
    ]


def category_map() -> dict[str, Category]:
    return {c["id"]: c for c in get_categories()}


def _escape(s: Any) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def _today_str() -> str:
    return date.today().strftime("%Y-%m-%d")


def _parse_date(d: str | None) -> str:
    v = (d or "").strip()
    if not v:
        return _today_str()
    try:
        dt = datetime.strptime(v, "%Y-%m-%d").date()
    except ValueError:
        return _today_str()
    return dt.strftime("%Y-%m-%d")


def _db_connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _db_init() -> None:
    with _db_connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS dishes (
              id          INTEGER PRIMARY KEY AUTOINCREMENT,
              name        TEXT NOT NULL,
              image_url   TEXT,
              category_id TEXT NOT NULL,
              chef        TEXT,
              ingredients TEXT,
              steps_json  TEXT,
              created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );

            CREATE TABLE IF NOT EXISTS menus (
              menu_date TEXT PRIMARY KEY,
              title     TEXT,
              created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );

            CREATE TABLE IF NOT EXISTS menu_items (
              menu_date TEXT NOT NULL,
              dish_id   INTEGER NOT NULL,
              position  INTEGER NOT NULL,
              PRIMARY KEY (menu_date, dish_id),
              FOREIGN KEY (menu_date) REFERENCES menus(menu_date) ON DELETE CASCADE,
              FOREIGN KEY (dish_id) REFERENCES dishes(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS ingredients (
              id          INTEGER PRIMARY KEY AUTOINCREMENT,
              name        TEXT NOT NULL,
              quantity    REAL,
              unit        TEXT,
              expires_on  TEXT,
              sealed      INTEGER NOT NULL DEFAULT 0,
              created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            """
        )


@dataclass(frozen=True)
class Dish:
    name: str
    image_url: str
    category_id: str
    chef: str
    ingredients: str
    steps: list[str]


def _insert_dish(dish: Dish) -> None:
    steps_json = json.dumps([s for s in dish.steps if s.strip()], ensure_ascii=False)
    with _db_connect() as conn:
        conn.execute(
            """
            INSERT INTO dishes (name, image_url, category_id, chef, ingredients, steps_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (dish.name, dish.image_url, dish.category_id, dish.chef, dish.ingredients, steps_json),
        )


def _list_dishes() -> list[sqlite3.Row]:
    with _db_connect() as conn:
        rows = conn.execute(
            """
            SELECT id, name, image_url, category_id, chef, ingredients, steps_json, created_at
            FROM dishes
            ORDER BY created_at DESC, id DESC
            """
        ).fetchall()
    return rows


def _get_dishes_by_ids(ids: list[int]) -> list[sqlite3.Row]:
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    with _db_connect() as conn:
        rows = conn.execute(
            f"SELECT id, name, image_url, category_id, chef, ingredients, steps_json FROM dishes WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
    # 按传入顺序排序
    id_order = {v: i for i, v in enumerate(ids)}
    rows.sort(key=lambda r: id_order.get(r["id"], 0))
    return rows


def _set_menu_for_date(menu_date: str, dish_ids: list[int], overwrite: bool) -> bool:
    """
    返回值：True=成功写入，False=已存在且未 overwrite
    """
    with _db_connect() as conn:
        existing = conn.execute("SELECT 1 FROM menus WHERE menu_date=?", (menu_date,)).fetchone()
        if existing and not overwrite:
            return False
        if not existing:
            conn.execute(
                "INSERT INTO menus (menu_date, title) VALUES (?, ?)",
                (menu_date, f"{menu_date} 的家庭菜谱"),
            )
        # 先清空原有
        conn.execute("DELETE FROM menu_items WHERE menu_date=?", (menu_date,))
        for pos, dish_id in enumerate(dish_ids, start=1):
            conn.execute(
                "INSERT INTO menu_items (menu_date, dish_id, position) VALUES (?, ?, ?)",
                (menu_date, dish_id, pos),
            )
    return True


def _get_menu(menu_date: str) -> tuple[sqlite3.Row | None, list[sqlite3.Row]]:
    with _db_connect() as conn:
        menu = conn.execute(
            "SELECT menu_date, title, created_at FROM menus WHERE menu_date=?",
            (menu_date,),
        ).fetchone()
        if not menu:
            return None, []
        items = conn.execute(
            """
            SELECT mi.dish_id, mi.position,
                   d.name, d.image_url, d.category_id, d.chef, d.ingredients, d.steps_json
            FROM menu_items mi
            JOIN dishes d ON d.id = mi.dish_id
            WHERE mi.menu_date=?
            ORDER BY mi.position ASC
            """,
            (menu_date,),
        ).fetchall()
    return menu, items


def _list_menu_dates_between(start_d: date, end_d: date) -> dict[str, dict[str, Any]]:
    with _db_connect() as conn:
        rows = conn.execute(
            """
            SELECT m.menu_date,
                   GROUP_CONCAT(DISTINCT d.category_id) AS category_ids
            FROM menus m
            JOIN menu_items mi ON mi.menu_date = m.menu_date
            JOIN dishes d ON d.id = mi.dish_id
            WHERE m.menu_date BETWEEN ? AND ?
            GROUP BY m.menu_date
            """,
            (start_d.strftime("%Y-%m-%d"), end_d.strftime("%Y-%m-%d")),
        ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        cats = (r["category_ids"] or "").split(",")
        out[r["menu_date"]] = {"categories": [c for c in cats if c]}
    return out


def _insert_ingredient(
    name: str,
    quantity: float | None,
    unit: str,
    expires_on: str | None,
) -> None:
    with _db_connect() as conn:
        conn.execute(
            """
            INSERT INTO ingredients (name, quantity, unit, expires_on)
            VALUES (?, ?, ?, ?)
            """,
            (name, quantity, unit, expires_on),
        )


def _list_ingredients() -> list[sqlite3.Row]:
    with _db_connect() as conn:
        rows = conn.execute(
            """
            SELECT id, name, quantity, unit, expires_on, sealed, created_at
            FROM ingredients
            ORDER BY
              CASE WHEN expires_on IS NULL THEN 1 ELSE 0 END,
              expires_on ASC,
              created_at DESC
            """
        ).fetchall()
    return rows


def _seal_ingredient(ing_id: int) -> None:
    today = date.today()
    with _db_connect() as conn:
        row = conn.execute(
            "SELECT expires_on, sealed FROM ingredients WHERE id=?",
            (ing_id,),
        ).fetchone()
        if not row:
            return
        expires_on = row["expires_on"]
        if not expires_on:
            return
        try:
            d = datetime.strptime(expires_on, "%Y-%m-%d").date()
        except ValueError:
            return
        remaining = (d - today).days
        if remaining < 0:
            remaining = 0
        new_expires = today + timedelta(days=remaining * 3)
        conn.execute(
            "UPDATE ingredients SET expires_on=?, sealed=1 WHERE id=?",
            (new_expires.strftime("%Y-%m-%d"), ing_id),
        )


def _layout(title: str, body_html: str, active: str = "dishes") -> str:
    cats = get_categories()
    nav_links = [
        ("dishes", "/dishes", "菜品管理"),
        ("ingredients", "/ingredients", "材料管理"),
        ("plan", "/plan", "计划管理"),
    ]

    nav_html = "".join(
        f'<a class="nav-link{" active" if active == key else ""}" href="{href}">{_escape(text)}</a>'
        for key, href, text in nav_links
    )

    bird_svg = """
<svg class="bird-svg" viewBox="0 0 64 64" aria-hidden="true" focusable="false">
  <defs>
    <linearGradient id="g1" x1="0" x2="1" y1="0" y2="1">
      <stop offset="0" stop-color="#FF6FB5"/>
      <stop offset="1" stop-color="#7C5CFF"/>
    </linearGradient>
    <linearGradient id="g2" x1="0" x2="1" y1="0" y2="1">
      <stop offset="0" stop-color="#34D399"/>
      <stop offset="1" stop-color="#22D3EE"/>
    </linearGradient>
  </defs>
  <path d="M22 46c-7.5-6.2-9.5-18.6-.8-27.1 8.6-8.4 24.1-7 30.7 2.8 5.5 8.2 2.2 20.8-8.3 25.3-6.6 2.8-11.7 1.9-21.6-1z" fill="url(#g2)"/>
  <path d="M24 43c-5.8-5.2-6.7-14.6.2-20.9 6.7-6.1 18.4-5.3 23.1 1.7 4.1 6 2 15.1-5.6 18.8-5.3 2.6-9.6 2.1-17.7.4z" fill="#FFF7FB"/>
  <path d="M29 36c-3.5-2.8-3.6-8.6 1.1-11.3 4.5-2.6 10.2-1 12.1 3.2 1.8 3.8-.4 8.5-4.8 9.8-3 .9-5.3.5-8.4-1.7z" fill="url(#g1)" opacity=".95"/>
  <path d="M52 28l8 4-8 4c-1.7-2.4-1.7-5.6 0-8z" fill="#FFB703"/>
  <circle cx="44" cy="27" r="2.2" fill="#1F1B4B"/>
  <circle cx="43.3" cy="26.3" r="0.8" fill="#FFFFFF"/>
  <circle cx="40" cy="31" r="2.2" fill="#FF6FB5" opacity=".25"/>
</svg>
""".strip()

    cat_badges = "".join(
        f'<span class="cat-pill" style="--pill-color:{c["color"]}">'
        f'<span class="cat-emoji">{_escape(c["emoji"])}</span>'
        f'<span class="cat-text">{_escape(c["name"])}</span>'
        "</span>"
        for c in cats
    )

    return f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{_escape(title)} · 家庭菜品小鸟系统</title>
    <link rel="stylesheet" href="/static/style.css" />
  </head>
  <body>
    <div class="container">
      <header class="header">
        <div class="brand">
          <div class="logo" title="家庭菜品小鸟系统">{bird_svg}</div>
          <div class="brand-text">
            <div class="app-title">家庭菜品 · 小鸟系统</div>
            <div class="app-subtitle">手机优先设计 · 在浏览器中使用</div>
            <div class="cat-row">{cat_badges}</div>
          </div>
        </div>
        <nav class="nav">{nav_html}</nav>
      </header>
      {body_html}
    </div>
  </body>
</html>
"""


def _dishes_page(message: str = "", error: str = "") -> str:
    cats = get_categories()
    cat_map = category_map()
    dishes = _list_dishes()
    msg_html = ""
    if message:
        msg_html = f'<div class="alert ok">{_escape(message)}</div>'
    if error:
        msg_html = f'<div class="alert err">{_escape(error)}</div>'

    options = "".join(
        f'<option value="{_escape(c["id"])}">{_escape(c["emoji"])} {_escape(c["name"])}</option>'
        for c in cats
    )

    items_html = []
    for d in dishes:
        cid = d["category_id"]
        meta = cat_map.get(cid, {"name": cid, "emoji": "🍽", "color": "#CBD5F5"})
        try:
            steps = json.loads(d["steps_json"] or "[]")
        except Exception:
            steps = []
        steps_text = " / ".join(str(s) for s in steps)
        items_html.append(
            "<label class='dish-card'>"
            f"<input class='dish-check' type='checkbox' name='dish_ids' value='{d['id']}' />"
            "<div class='dish-main'>"
            "<div class='dish-head'>"
            f"<div class='dish-cat' style='--pill-color:{meta['color']}'><span class='cat-emoji'>{_escape(meta['emoji'])}</span>"
            f"<span class='cat-text'>{_escape(meta['name'])}</span></div>"
            f"<div class='dish-name'>{_escape(d['name'])}</div>"
            "</div>"
            "<div class='dish-meta'>"
            f"<span class='meta-item'>👨‍🍳 {_escape(d['chef'] or '未指定')}</span>"
            "</div>"
            "<div class='dish-bottom'>"
            f"<div class='meta-item small'>材料：{_escape((d['ingredients'] or '')[:80])}</div>"
            f"<div class='meta-item small'>步骤：{_escape(steps_text[:80])}</div>"
            "</div>"
            "</div>"
            "</label>"
        )

    body = f"""
<main class="main">
  <section class="card">
    <h1 class="h1">菜品管理</h1>
    <p class="muted small">为家里的菜品建档，勾选多个菜品，一键生成「今日菜谱」。</p>
    {msg_html}
    <form class="form" method="post" action="/dishes/new" id="dish-form">
      <h2 class="h2">新增菜品</h2>
      <div class="field">
        <span class="label">菜品名称</span>
        <input type="text" name="name" required maxlength="100" placeholder="例如：番茄牛腩" />
      </div>
      <div class="field">
        <span class="label">菜品预览图链接（可空）</span>
        <input type="url" name="image_url" placeholder="可以直接粘贴手机相册上传后的图片链接" />
      </div>
      <div class="grid">
        <label class="field">
          <span class="label">菜品分类</span>
          <select name="category_id" required>
            {options}
          </select>
        </label>
        <label class="field">
          <span class="label">主厨</span>
          <input type="text" name="chef" placeholder="例如：妈妈 / 爸爸 / 小朋友" />
        </label>
      </div>
      <label class="field">
        <span class="label">材料</span>
        <textarea name="ingredients" rows="3" placeholder="例如：番茄 2 个，牛腩 500g，洋葱 1/4 个..."></textarea>
      </label>
      <div class="field">
        <div class="label-row">
          <span class="label">步骤</span>
          <button type="button" class="btn pill-btn" onclick="addStep()">＋ 新增步骤</button>
        </div>
        <div id="step-list" class="step-list">
          <div class="step-item">
            <span class="step-index">1</span>
            <input type="text" name="steps" placeholder="例如：牛腩冷水下锅焯水" />
          </div>
        </div>
      </div>
      <div class="actions">
        <button class="btn primary" type="submit">保存菜品</button>
      </div>
    </form>
  </section>

  <section class="card">
    <div class="today-head">
      <div>
        <h2 class="h2">生成今日菜谱</h2>
        <p class="muted small">勾选下方菜品，选择日期，生成一个可截图保存的「今日菜谱」界面。</p>
      </div>
      <form class="today-form" method="post" action="/menus/generate" onsubmit="return onSubmitMenu()">
        <label class="field small-field">
          <span class="label">日期</span>
          <input type="date" name="menu_date" value="{_escape(_today_str())}" required />
        </label>
        <input type="hidden" name="dish_ids_joined" id="dish_ids_joined" />
        <input type="hidden" name="force" id="force" value="0" />
        <button class="btn primary" type="submit">生成今日菜谱</button>
      </form>
    </div>

    <div class="dish-list" id="dish-list">
      {''.join(items_html) if items_html else '<div class="muted small">还没有菜品，请先在上面新增。</div>'}
    </div>
  </section>
</main>

<script>
function addStep() {{
  const list = document.getElementById('step-list');
  const idx = list.children.length + 1;
  const div = document.createElement('div');
  div.className = 'step-item';
  div.innerHTML = '<span class="step-index">' + idx + '</span>' +
                  '<input type="text" name="steps" placeholder="步骤 ' + idx + '" />';
  list.appendChild(div);
}}

function collectCheckedIds() {{
  const checks = document.querySelectorAll('.dish-check:checked');
  const ids = [];
  checks.forEach(c => ids.push(c.value));
  return ids;
}}

function onSubmitMenu() {{
  const ids = collectCheckedIds();
  if (ids.length === 0) {{
    alert('请先勾选至少一个菜品。');
    return false;
  }}
  document.getElementById('dish_ids_joined').value = ids.join(',');
  return true;
}}
</script>
"""
    return _layout("菜品管理", body, active="dishes")


def _menu_page(menu_date: str) -> str:
    menu, items = _get_menu(menu_date)
    cat_map = category_map()
    if not menu:
        body = f"""
<main class="main">
  <section class="card">
    <h1 class="h1">今日菜谱</h1>
    <p class="muted">日期：<span class="mono">{_escape(menu_date)}</span></p>
    <div class="alert err">这一天还没有生成菜谱，可以在「菜品管理」中勾选菜品后生成。</div>
  </section>
</main>
"""
        return _layout("今日菜谱", body, active="dishes")

    cards = []
    for i, it in enumerate(items, start=1):
        meta = cat_map.get(it["category_id"], {"name": it["category_id"], "emoji": "🍽", "color": "#CBD5F5"})
        try:
            steps = json.loads(it["steps_json"] or "[]")
        except Exception:
            steps = []
        step_html = "".join(
            f"<li><span class='step-no'>{idx + 1}</span><span>{_escape(s)}</span></li>"
            for idx, s in enumerate(steps)
        )
        fallback_step = "<li><span class='step-no'>1</span><span>自由发挥～</span></li>"
        step_list_html = step_html or fallback_step
        cards.append(
            "<article class='dish-sheet-card'>"
            "<header class='dish-sheet-head'>"
            f"<div class='pill' style='--pill-color:{meta['color']}'><span class='cat-emoji'>{_escape(meta['emoji'])}</span><span class='cat-text'>{_escape(meta['name'])}</span></div>"
            f"<h2 class='dish-sheet-title'>{i}. {_escape(it['name'])}</h2>"
            f"<div class='small muted'>主厨：{_escape(it['chef'] or '未指定')}</div>"
            "</header>"
            "<div class='dish-sheet-body'>"
            f"<div class='dish-sheet-section'><div class='section-title'>材料</div><p class='section-text'>{_escape(it['ingredients'] or '——')}</p></div>"
            "<div class='dish-sheet-section'>"
            "<div class='section-title'>步骤</div>"
            f"<ol class='step-ol'>{step_list_html}</ol>"
            "</div>"
            "</div>"
            "</article>"
        )

    body = f"""
<main class="main">
  <section class="card">
    <div class="sheet-head">
      <div>
        <h1 class="h1">今日菜谱</h1>
        <p class="muted">日期：<span class="mono">{_escape(menu_date)}</span> · 共 {len(items)} 道菜</p>
        <p class="muted small">提示：在手机上可以长按本页面截图，保存成图片。</p>
      </div>
      <div class="sheet-actions">
        <a class="btn" href="/dishes">返回菜品管理</a>
      </div>
    </div>
    <div class="sheet" id="sheet">
      {''.join(cards)}
    </div>
  </section>
</main>
"""
    return _layout("今日菜谱", body, active="dishes")


def _ingredients_page(message: str = "", error: str = "") -> str:
    rows = _list_ingredients()
    msg_html = ""
    if message:
        msg_html = f'<div class="alert ok">{_escape(message)}</div>'
    if error:
        msg_html = f'<div class="alert err">{_escape(error)}</div>'

    def status_text(r: sqlite3.Row) -> str:
        exp = (r["expires_on"] or "").strip()
        if not exp:
            return "未设置"
        try:
            d = datetime.strptime(exp, "%Y-%m-%d").date()
        except ValueError:
            return "格式错误"
        diff = (d - date.today()).days
        if diff < 0:
            return f"已过期 {abs(diff)} 天"
        if diff == 0:
            return "今天到期"
        if diff <= 3:
            return f"即将过期（{diff} 天）"
        return f"剩余 {diff} 天"

    items = []
    for r in rows:
        status = status_text(r)
        sealed = bool(r["sealed"])
        if sealed:
            action_html = "已密封"
        else:
            action_html = (
                "<form method=\"post\" action=\"/ingredients/seal\" "
                "onsubmit=\"return confirm('确认密封并延长有效期 x3 吗？')\">"
                f"<input type=\"hidden\" name=\"id\" value=\"{_escape(r['id'])}\" />"
                "<button class=\"btn pill-btn\" type=\"submit\">密封 x3</button>"
                "</form>"
            )
        items.append(
            "<tr>"
            f"<td>{_escape(r['name'])}</td>"
            f"<td class='mono'>{_escape(r['quantity'])} {_escape(r['unit'] or '')}</td>"
            f"<td class='mono'>{_escape(r['expires_on'] or '—')}</td>"
            f"<td>{_escape(status)}</td>"
            f"<td>{action_html}</td>"
            "</tr>"
        )

    body = f"""
<main class="main">
  <section class="card">
    <h1 class="h1">材料管理</h1>
    <p class="muted small">记录家里的关键材料，支持设置有效期与数量，一键「密封」将剩余有效期延长 3 倍。</p>
    {msg_html}
    <form class="form" method="post" action="/ingredients/new">
      <div class="grid">
        <label class="field">
          <span class="label">材料名称</span>
          <input type="text" name="name" required maxlength="100" placeholder="例如：鸡腿 / 豆腐 / 西兰花" />
        </label>
        <label class="field">
          <span class="label">数量（可空）</span>
          <input type="number" step="0.01" name="quantity" placeholder="例如：2 / 500" />
        </label>
        <label class="field">
          <span class="label">单位（可空）</span>
          <input type="text" name="unit" placeholder="例如：个 / g / 袋" />
        </label>
        <label class="field">
          <span class="label">有效期（可空）</span>
          <input type="date" name="expires_on" />
        </label>
      </div>
      <div class="actions">
        <button class="btn primary" type="submit">添加材料</button>
      </div>
    </form>
  </section>

  <section class="card">
    <h2 class="h2">材料列表</h2>
    <div class="table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th>名称</th>
            <th>数量</th>
            <th>有效期</th>
            <th>状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {''.join(items) if items else '<tr><td colspan="5" class="muted small">目前还没有材料记录。</td></tr>'}
        </tbody>
      </table>
    </div>
  </section>
</main>
"""
    return _layout("材料管理", body, active="ingredients")


def _plan_page(view: str, focus_date: str | None = None) -> str:
    view = view or "month"
    if view not in ("week", "month", "year"):
        view = "month"
    today = date.today()
    if focus_date:
        try:
            center = datetime.strptime(focus_date, "%Y-%m-%d").date()
        except ValueError:
            center = today
    else:
        center = today

    cat_map = category_map()

    if view == "week":
        start = center - timedelta(days=center.weekday())
        end = start + timedelta(days=6)
    elif view == "year":
        start = date(center.year, 1, 1)
        end = date(center.year, 12, 31)
    else:
        first = center.replace(day=1)
        start = first - timedelta(days=first.weekday())
        end = (first + timedelta(days=40)).replace(day=1) - timedelta(days=1)

    menu_marks = _list_menu_dates_between(start, end)

    # 简化：统一采用月视图网格，周/年只影响时间范围与标题
    cells = []
    cur = start
    while cur <= end:
        ds = cur.strftime("%Y-%m-%d")
        info = menu_marks.get(ds)
        marks = ""
        if info:
            cats = info.get("categories") or []
            seen = set()
            pills = []
            for cid in cats:
                if cid in seen:
                    continue
                seen.add(cid)
                meta = cat_map.get(cid, {"emoji": "🍽", "color": "#CBD5F5"})
                pills.append(
                    f"<span class='mini-pill' style='--pill-color:{meta['color']}' title='{_escape(meta['name'])}'>{_escape(meta['emoji'])}</span>"
                )
            marks = "<div class='mini-row'>" + "".join(pills) + "</div>"
        is_today = cur == today
        classes = ["cal-cell"]
        if cur.month != center.month:
            classes.append("muted-cell")
        if is_today:
            classes.append("today")
        cells.append(
            f"<a class='{' '.join(classes)}' href='/menus/today?date={ds}'>"
            f"<div class='cal-date'>{cur.day}</div>{marks}</a>"
        )
        cur += timedelta(days=1)

    weeks_html = ""
    for i in range(0, len(cells), 7):
        weeks_html += "<div class='cal-row'>" + "".join(cells[i : i + 7]) + "</div>"

    title = {
        "week": "周计划",
        "month": "月计划",
        "year": "年计划",
    }[view]

    body = f"""
<main class="main">
  <section class="card">
    <div class="plan-head">
      <div>
        <h1 class="h1">计划管理 · {title}</h1>
        <p class="muted small">在日历上查看每天的「今日菜谱」，小图标来自菜品分类。</p>
      </div>
      <div class="plan-tabs">
        <a class="tab{' active' if view=='week' else ''}" href="/plan?view=week">周</a>
        <a class="tab{' active' if view=='month' else ''}" href="/plan?view=month">月</a>
        <a class="tab{' active' if view=='year' else ''}" href="/plan?view=year">年</a>
      </div>
    </div>
    <div class="cal">
      <div class="cal-row cal-head">
        <div>一</div><div>二</div><div>三</div><div>四</div><div>五</div><div>六</div><div>日</div>
      </div>
      {weeks_html}
    </div>
  </section>
</main>
"""
    return _layout("计划管理", body, active="plan")


class Handler(BaseHTTPRequestHandler):
    server_version = "HomeMenu/1.0"

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length) if length > 0 else b""

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query or "")

        if path in ("/", "/dishes"):
            msg = (query.get("msg") or [""])[0]
            err = (query.get("err") or [""])[0]
            html_text = _dishes_page(message=msg, error=err)
            self._send(HTTPStatus.OK, html_text.encode("utf-8"), "text/html; charset=utf-8")
            return

        if path == "/menus/today":
            menu_date = _parse_date((query.get("date") or [""])[0])
            html_text = _menu_page(menu_date)
            self._send(HTTPStatus.OK, html_text.encode("utf-8"), "text/html; charset=utf-8")
            return

        if path == "/ingredients":
            msg = (query.get("msg") or [""])[0]
            err = (query.get("err") or [""])[0]
            html_text = _ingredients_page(message=msg, error=err)
            self._send(HTTPStatus.OK, html_text.encode("utf-8"), "text/html; charset=utf-8")
            return

        if path == "/plan":
            view = (query.get("view") or [""])[0] or "month"
            html_text = _plan_page(view=view)
            self._send(HTTPStatus.OK, html_text.encode("utf-8"), "text/html; charset=utf-8")
            return

        if path.startswith("/static/"):
            rel = path.removeprefix("/static/").lstrip("/")
            target = (STATIC_DIR / rel).resolve()
            if not str(target).startswith(str(STATIC_DIR.resolve())):
                self._send(HTTPStatus.NOT_FOUND, b"Not Found", "text/plain; charset=utf-8")
                return
            if not target.exists() or not target.is_file():
                self._send(HTTPStatus.NOT_FOUND, b"Not Found", "text/plain; charset=utf-8")
                return
            ctype = "text/plain; charset=utf-8"
            if target.suffix == ".css":
                ctype = "text/css; charset=utf-8"
            data = target.read_bytes()
            self._send(HTTPStatus.OK, data, ctype)
            return

        self._send(HTTPStatus.NOT_FOUND, b"Not Found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._read_body()
        form = parse_qs(body.decode("utf-8", errors="replace"))

        def f(name: str) -> str:
            return (form.get(name) or [""])[0]

        if path == "/dishes/new":
            name = f("name").strip()
            if not name:
                self._redirect("/dishes?" + urlencode({"err": "菜品名称不能为空"}))
                return
            dish = Dish(
                name=name,
                image_url=f("image_url").strip(),
                category_id=f("category_id").strip() or "home",
                chef=f("chef").strip(),
                ingredients=f("ingredients").strip(),
                steps=[s for s in form.get("steps", []) if s.strip()],
            )
            try:
                _insert_dish(dish)
            except sqlite3.Error:
                self._redirect("/dishes?" + urlencode({"err": "保存菜品失败，请稍后重试"}))
                return
            self._redirect("/dishes?" + urlencode({"msg": "已保存菜品"}))
            return

        if path == "/menus/generate":
            menu_date = _parse_date(f("menu_date"))
            ids_joined = f("dish_ids_joined")
            ids = [int(x) for x in ids_joined.split(",") if x.strip().isdigit()]
            if not ids:
                self._redirect("/dishes?" + urlencode({"err": "请至少勾选一个菜品"}))
                return
            force = f("force") == "1"
            ok = _set_menu_for_date(menu_date, ids, overwrite=force)
            if not ok:
                # 已存在，弹出确认提示（通过 query 带给前端）
                qs = urlencode(
                    {
                        "err": f"{menu_date} 已经有菜谱，若继续生成将覆盖，是否继续？",
                    }
                )
                self._redirect("/dishes?" + qs)
                return
            self._redirect("/menus/today?date=" + menu_date)
            return

        if path == "/ingredients/new":
            name = f("name").strip()
            if not name:
                self._redirect("/ingredients?" + urlencode({"err": "材料名称不能为空"}))
                return
            quantity_raw = f("quantity").strip()
            quantity: float | None
            if quantity_raw:
                try:
                    quantity = float(quantity_raw)
                except ValueError:
                    self._redirect("/ingredients?" + urlencode({"err": "数量必须是数字"}))
                    return
            else:
                quantity = None
            unit = f("unit").strip()
            expires_on_val = f("expires_on").strip() or None
            if expires_on_val:
                try:
                    datetime.strptime(expires_on_val, "%Y-%m-%d")
                except ValueError:
                    self._redirect("/ingredients?" + urlencode({"err": "有效期格式不正确"}))
                    return
            try:
                _insert_ingredient(name, quantity, unit, expires_on_val)
            except sqlite3.Error:
                self._redirect("/ingredients?" + urlencode({"err": "保存材料失败"}))
                return
            self._redirect("/ingredients?" + urlencode({"msg": "已添加材料"}))
            return

        if path == "/ingredients/seal":
            ing_id_raw = f("id").strip()
            if not ing_id_raw.isdigit():
                self._redirect("/ingredients?" + urlencode({"err": "ID 不合法"}))
                return
            _seal_ingredient(int(ing_id_raw))
            self._redirect("/ingredients?" + urlencode({"msg": "已密封，剩余有效期 x3"}))
            return

        self._send(HTTPStatus.NOT_FOUND, b"Not Found", "text/plain; charset=utf-8")


def main() -> None:
    _db_init()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"已启动： http://{HOST}:{PORT}/dishes")
    print(f"今日菜谱： http://{HOST}:{PORT}/menus/today")
    print(f"材料管理： http://{HOST}:{PORT}/ingredients")
    print(f"计划管理： http://{HOST}:{PORT}/plan")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

