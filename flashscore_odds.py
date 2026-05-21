from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Tuple

FLASHSCORE_TENNIS_URL = "https://www.flashscore.fr/tennis/"

def _norm_name(value: str) -> str:
    value = value or ""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower()
    value = re.sub(r"\[[^\]]+\]", " ", value)
    value = re.sub(r"\([^)]*\)", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = re.sub(r"\b(?:wc|q|ll|pr|alt|seed)\b", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value

def _name_tokens(value: str) -> List[str]:
    return [x for x in _norm_name(value).split() if len(x) >= 2]

def _same_player(a: str, b: str) -> bool:
    """
    Comparaison joueurs robuste pour SportyTrader.

    SportyTrader affiche parfois seulement le nom de famille :
    - "Hanfmann" au lieu de "Yannick Hanfmann"
    - "Darderi" au lieu de "Luciano Darderi"

    Le moteur ne l'utilise pas. Cette fonction sert seulement à accrocher la cote
    au bon match pour l'affichage Unity.
    """
    na = _norm_name(a)
    nb = _norm_name(b)

    if not na or not nb:
        return False

    if na == nb:
        return True

    ta = _name_tokens(a)
    tb = _name_tokens(b)

    if not ta or not tb:
        return False

    if set(ta) == set(tb):
        return True

    last_a = ta[-1]
    last_b = tb[-1]

    # Cas SportyTrader fréquent : un côté = nom de famille uniquement.
    if len(ta) == 1 and len(ta[0]) >= 4 and ta[0] == last_b:
        return True

    if len(tb) == 1 and len(tb[0]) >= 4 and tb[0] == last_a:
        return True

    # Cas nom composé partiel : "Mpetshi Perricard" / "Giovanni Mpetshi Perricard".
    if len(ta) >= 2 and len(tb) >= 2:
        tail_a = " ".join(ta[-2:])
        tail_b = " ".join(tb[-2:])
        if tail_a == tail_b:
            return True

    if last_a == last_b:
        first_a = ta[0][0]
        first_b = tb[0][0]
        return first_a == first_b or ta[0] in tb or tb[0] in ta

    return False

def _flashscore_click_optional(page, labels: List[str], timeout_ms: int = 1500) -> bool:
    for label in labels:
        selectors = [
            f"text={label}",
            f"button:has-text('{label}')",
            f"[role='button']:has-text('{label}')",
        ]
        for selector in selectors:
            try:
                page.locator(selector).first.click(timeout=timeout_ms)
                return True
            except Exception:
                pass
    return False

def _parse_odd_text(value: str) -> str:
    """
    Parse strict des cotes européennes.

    Accepte uniquement :
    - 1.56
    - 2.52
    - 1,35

    Refuse explicitement :
    - scores : 6, 3, 2, 0
    - scores tennis : 2/6, 15/40, 6/3
    - textes mélangés
    """
    raw = (value or "").strip()

    if "/" in raw:
        return ""

    if not re.fullmatch(r"\d{1,2}[.,]\d{1,2}", raw):
        return ""

    normalized = raw.replace(",", ".")

    try:
        number = float(normalized)
    except Exception:
        return ""

    if number < 1.01 or number > 100.0:
        return ""

    return f"{number:.2f}".rstrip("0").rstrip(".")

def _flashscore_extract_rows_js() -> str:
    return r"""
() => {
    const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();

    const readText = (root, selectors) => {
        for (const sel of selectors) {
            const el = root.querySelector(sel);
            if (el) {
                const t = clean(el.textContent);
                if (t) return t;
            }
        }
        return '';
    };

    const readOdds = (root) => {
        const odds = [];
        const seen = new Set();

        const addOdd = (txt) => {
            txt = clean(txt);
            if (!txt || seen.has(txt)) return;

            // Cotes européennes uniquement : 1.40 / 2.36 / 10.5
            if (/^\d{1,2}[.,]\d{1,2}$/.test(txt)) {
                const val = parseFloat(txt.replace(',', '.'));
                if (val >= 1.01 && val <= 100) {
                    seen.add(txt);
                    odds.push(txt);
                }
            }
        };

        const parseOddsFromText = (txt) => {
            txt = clean(txt);
            if (!txt) return;

            // Extrait 1.40 / 2.36 / 10,5 depuis le texte brut du bloc match.
            const re = /(^|[^\d])(\d{1,2}[.,]\d{1,2})(?!\d)/g;
            let m;
            while ((m = re.exec(txt)) !== null) {
                addOdd(m[2]);
                if (odds.length >= 2) return;
            }
        };

        const selectors = [
            '[class*="event__odd"]',
            '[class*="oddsValue"]',
            '[class*="odds"]',
            '[class*="bookmaker"]',
            'button',
            'span',
            'div'
        ];

        for (const sel of selectors) {
            root.querySelectorAll(sel).forEach((el) => {
                addOdd(el.textContent);
            });
            if (odds.length >= 2) break;
        }

        // Fallback important : sur Flashscore, les cotes sont parfois dans le
        // texte du bloc ligne mais sans classe "event__odd" stable.
        if (odds.length < 2) {
            parseOddsFromText(root.innerText || root.textContent || '');
        }

        // Fallback parent : selon la structure DOM, les cotes peuvent être dans
        // un conteneur voisin du participant.
        if (odds.length < 2 && root.parentElement) {
            parseOddsFromText(root.parentElement.innerText || root.parentElement.textContent || '');
        }

        if (odds.length < 2 && root.parentElement && root.parentElement.parentElement) {
            parseOddsFromText(root.parentElement.parentElement.innerText || root.parentElement.parentElement.textContent || '');
        }

        return odds.slice(0, 2);
    };

    const rows = [];
    const matchSelectors = [
        '[class*="event__match"]',
        '[id^="g_2_"]',
        '[id^="g_1_"]'
    ];

    let nodes = [];
    for (const sel of matchSelectors) {
        nodes = Array.from(document.querySelectorAll(sel));
        if (nodes.length) break;
    }

    for (const node of nodes) {
        const playerA = readText(node, [
            '[class*="event__participant--home"]',
            '[class*="participant__participantNameWrapper"]:nth-of-type(1)',
            '[class*="participantName"]:nth-of-type(1)'
        ]);

        const playerB = readText(node, [
            '[class*="event__participant--away"]',
            '[class*="participant__participantNameWrapper"]:nth-of-type(2)',
            '[class*="participantName"]:nth-of-type(2)'
        ]);

        const time = readText(node, [
            '[class*="event__time"]',
            '[class*="event__stage"]'
        ]);

        const odds = readOdds(node);

        if (playerA && playerB) {
            rows.push({
                playerA,
                playerB,
                oddA: odds[0] || '',
                oddB: odds[1] || '',
                time,
                raw: clean(node.innerText || node.textContent || '')
            });
        }
    }

    return rows;
}
"""

def _looks_like_flashscore_player_line(line: str) -> bool:
    line = re.sub(r"\s+", " ", line or "").strip()

    if not line or len(line) > 70:
        return False

    low = line.lower()
    banned = [
        "atp", "wta", "simples", "doubles", "classement", "publicité",
        "preview", "live", "bet", "terminé", "annulé", "reporté",
        "tous", "direct", "cotes", "prévus", "prevus", "calendrier"
    ]

    if any(x in low for x in banned):
        return False

    if re.fullmatch(r"\d+", line):
        return False

    if re.fullmatch(r"\d{1,2}:\d{2}", line):
        return False

    if _parse_odd_text(line):
        return False

    return bool(re.search(r"[A-Za-zÀ-ÿ]", line))

def _flashscore_extract_rows_from_text(content: str) -> List[Dict[str, str]]:
    """
    Fallback texte score-aware.

    Corrige le cas visible dans ton navigateur :
    Terminé / Ruud C. / 2 / 6 / 6 / Lehecka J. / 0 / 3 / 4 / 1.56 / 2.52

    On refuse les scores entiers et on cherche les deux premières vraies cotes décimales.
    """
    lines = [re.sub(r"\s+", " ", x).strip() for x in (content or "").splitlines()]
    lines = [x for x in lines if x]

    rows: List[Dict[str, str]] = []
    seen = set()

    def collect_decimal_odds(start_index: int, end_index: int) -> List[str]:
        odds: List[str] = []
        for candidate in lines[start_index:end_index]:
            parsed = _parse_odd_text(candidate)
            if parsed and parsed not in odds:
                odds.append(parsed)
            if len(odds) >= 2:
                break
        return odds

    for i in range(len(lines) - 1):
        player_a = lines[i]
        player_b = lines[i + 1]

        direct_pair = (
            _looks_like_flashscore_player_line(player_a)
            and _looks_like_flashscore_player_line(player_b)
        )

        score_pair_index = -1

        if not direct_pair and _looks_like_flashscore_player_line(player_a):
            # Match terminé/live : le joueur B peut être après des scores entiers.
            for j in range(i + 2, min(i + 9, len(lines))):
                between = lines[i + 1:j]
                if not between:
                    continue

                only_scores = all(re.fullmatch(r"\d+", x or "") for x in between)
                if only_scores and _looks_like_flashscore_player_line(lines[j]):
                    score_pair_index = j
                    player_b = lines[j]
                    break

        if not direct_pair and score_pair_index < 0:
            continue

        key = (_norm_name(player_a), _norm_name(player_b))
        if not key[0] or not key[1] or key in seen:
            continue

        seen.add(key)

        odds_start = score_pair_index + 1 if score_pair_index >= 0 else i + 2
        odds = collect_decimal_odds(odds_start, min(odds_start + 22, len(lines)))

        rows.append({
            "playerA": player_a,
            "playerB": player_b,
            "oddA": odds[0] if len(odds) > 0 else "",
            "oddB": odds[1] if len(odds) > 1 else "",
            "time": "",
            "raw": " | ".join(lines[i:min(i + 22, len(lines))]),
        })

    return rows

def _flashscore_tokens_keep_initials(name: str) -> List[str]:
    """
    Tokens Flashscore sans supprimer les initiales.
    Exemple :
    - "Cilic M." -> ["cilic", "m"]
    - "Auger-Aliassime F." -> ["auger", "aliassime", "f"]
    """
    value = _norm_name(name)
    return [x for x in value.split() if x]

def _normalize_flashscore_initial_name(name: str) -> List[str]:
    return _flashscore_tokens_keep_initials(name)

def _same_player_flashscore(full_name: str, flash_name: str) -> bool:
    """
    Matching robuste ATP full name <-> Flashscore.
    Corrige le bug principal :
    _name_tokens supprimait les initiales d'une lettre, donc "Cilic M."
    devenait seulement ["cilic"] et les cas nom composé + initiale étaient mal reconnus.
    """
    if _same_player(full_name, flash_name):
        return True

    full = _flashscore_tokens_keep_initials(full_name)
    flash = _flashscore_tokens_keep_initials(flash_name)

    if not full or not flash:
        return False

    full_first_initial = full[0][0] if full[0] else ""
    full_last = full[-1]
    full_tail_2 = " ".join(full[-2:]) if len(full) >= 2 else full_last
    full_tail_3 = " ".join(full[-3:]) if len(full) >= 3 else full_tail_2

    # Cas exact après normalisation.
    if " ".join(full) == " ".join(flash):
        return True

    # Flashscore : "Cilic M.", "Norrie C.", "Auger Aliassime F."
    if len(flash) >= 2 and len(flash[-1]) == 1:
        initial = flash[-1]
        surname_parts = flash[:-1]
        surname = " ".join(surname_parts)

        if initial == full_first_initial:
            # Nom composé complet : Felix Auger Aliassime <-> Auger Aliassime F.
            if surname == full_tail_2 or surname == full_tail_3:
                return True

            # Sécurité nom de famille simple : Marin Cilic <-> Cilic M.
            if surname_parts and surname_parts[-1] == full_last:
                return True

    # Flashscore peut parfois afficher nom de famille seul.
    if len(flash) == 1 and len(flash[0]) >= 4:
        if flash[0] == full_last:
            return True
        if flash[0] in full:
            return True

    # Cas sans initiale mais nom composé partiel.
    flash_join = " ".join(flash)
    if len(flash) >= 2:
        if flash_join == full_tail_2 or flash_join == full_tail_3:
            return True

    return False

def _flashscore_count_match_nodes_js() -> str:
    return r"""
() => {
    const selectors = [
        '[class*="event__match"]',
        '[id^="g_2_"]',
        '[id^="g_1_"]'
    ];

    for (const sel of selectors) {
        const nodes = document.querySelectorAll(sel);
        if (nodes && nodes.length) return nodes.length;
    }

    return 0;
}
"""

def _flashscore_scroll_until_stable(page, audit: List[str], max_rounds: int = 22) -> None:
    """
    Scroll réel de la page Flashscore.

    Objectif :
    - charger les matchs plus bas dans la page ;
    - attendre le lazy-load ;
    - arrêter seulement quand le nombre de lignes ne monte plus.
    """
    last_count = -1
    stable_rounds = 0

    for round_index in range(max_rounds):
        try:
            current_count = int(page.evaluate(_flashscore_count_match_nodes_js()))
        except Exception:
            current_count = -1

        audit.append(f"scroll_round={round_index + 1} rows_before={current_count}")

        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass

        try:
            page.mouse.wheel(0, 1400)
        except Exception:
            pass

        page.wait_for_timeout(900)

        try:
            new_count = int(page.evaluate(_flashscore_count_match_nodes_js()))
        except Exception:
            new_count = current_count

        audit.append(f"scroll_round={round_index + 1} rows_after={new_count}")

        if new_count <= last_count or new_count == current_count:
            stable_rounds += 1
        else:
            stable_rounds = 0

        last_count = max(last_count, new_count)

        # 3 tours sans nouvelle ligne = page chargée.
        if stable_rounds >= 3:
            break

    # Remonte légèrement en haut pour garder la page stable avant extraction finale.
    try:
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(500)
    except Exception:
        pass

def fetch_flashscore_tennis_odds() -> Tuple[List[Dict[str, str]], str]:
    audit: List[str] = []

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return [], f"playwright_import_error={type(exc).__name__}: {exc}"

    rows: List[Dict[str, str]] = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )

            context = browser.new_context(
                locale="fr-FR",
                timezone_id="Europe/Paris",
                viewport={"width": 1365, "height": 1800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                extra_http_headers={
                    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
                },
            )

            page = context.new_page()
            page.goto(FLASHSCORE_TENNIS_URL, wait_until="domcontentloaded", timeout=45000)

            _flashscore_click_optional(page, ["J'accepte", "Tout refuser", "Accepter", "OK"], timeout_ms=2500)

            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            # V4 :
            # Scanner plusieurs onglets au lieu de seulement COTES.
            # Flashscore ne met pas toujours tous les matchs du jour dans le même onglet.
            # On combine TOUS + COTES + PRÉVUS pour récupérer plus de paires.
            all_rows: List[Dict[str, str]] = []
            seen_rows = set()

            tab_sets = [
                ["TOUS", "Tous"],
                ["COTES", "Cotes"],
                ["PRÉVUS", "PREVUS", "Prévus", "Prevus"],
            ]

            for tab_labels in tab_sets:
                clicked = _flashscore_click_optional(page, tab_labels, timeout_ms=2500)
                page.wait_for_timeout(2200)

                audit.append(f"tab_scan={tab_labels[0]} clicked={clicked}")

                # Scroll pour charger toutes les lignes de cet onglet.
                _flashscore_scroll_until_stable(page, audit, max_rounds=18)

                tab_rows: List[Dict[str, str]] = []

                try:
                    tab_rows = page.evaluate(_flashscore_extract_rows_js())
                    audit.append(f"tab={tab_labels[0]} dom_rows={len(tab_rows)}")
                except Exception as exc:
                    audit.append(f"tab={tab_labels[0]} dom_extract_error={type(exc).__name__}: {exc}")
                    tab_rows = []

                # Toujours ajouter le fallback texte : les vraies cotes peuvent être
                # dans le texte global même si le sous-bloc DOM de la ligne ne les contient pas.
                try:
                    content = page.locator("body").inner_text(timeout=15000)
                    text_rows = _flashscore_extract_rows_from_text(content)
                    audit.append(f"tab={tab_labels[0]} text_rows={len(text_rows)} content_len={len(content)}")
                    tab_rows.extend(text_rows)
                except Exception as exc:
                    audit.append(f"tab={tab_labels[0]} text_extract_error={type(exc).__name__}: {exc}")

                for row in tab_rows:
                    a = str(row.get("playerA", "")).strip()
                    b = str(row.get("playerB", "")).strip()
                    oa = str(row.get("oddA", "")).strip()
                    ob = str(row.get("oddB", "")).strip()

                    if not a or not b:
                        continue

                    # On garde prioritairement les lignes qui ont des cotes.
                    # Les lignes sans cotes servent rarement à l'affichage.
                    key_pair = (_norm_name(a), _norm_name(b))
                    if not key_pair[0] or not key_pair[1]:
                        continue

                    replaced = False
                    for idx, existing in enumerate(all_rows):
                        existing_pair = (_norm_name(existing.get("playerA", "")), _norm_name(existing.get("playerB", "")))
                        if existing_pair == key_pair:
                            existing_has_odds = bool(existing.get("oddA")) and bool(existing.get("oddB"))
                            new_has_odds = bool(oa) and bool(ob)
                            if new_has_odds and not existing_has_odds:
                                all_rows[idx] = row
                            replaced = True
                            break

                    if replaced:
                        continue

                    all_rows.append(row)

            rows = all_rows
            audit.append(f"dom_rows_combined={len(rows)}")
            audit.append("odds_decimal_only_filter=on")
            audit.append("text_fallback_score_aware=on")
            audit.append("strict_parse_no_slash=on")

            if rows:
                sample = []
                for row in rows[:15]:
                    sample.append(f"{row.get('playerA')} - {row.get('playerB')} = {row.get('oddA')}/{row.get('oddB')}")
                audit.append("sample=" + " || ".join(sample))

            browser.close()

    except Exception as exc:
        audit.append(f"flashscore_error={type(exc).__name__}: {exc}")

    # Nettoyage : garder seulement les lignes avec deux joueurs.
    clean_rows: List[Dict[str, str]] = []
    seen = set()

    for row in rows:
        a = str(row.get("playerA", "")).strip()
        b = str(row.get("playerB", "")).strip()

        if not a or not b:
            continue
        if "/" in a or "/" in b:
            continue

        key = (_norm_name(a), _norm_name(b))
        if key in seen:
            continue
        seen.add(key)

        clean_rows.append({
            "playerA": a,
            "playerB": b,
            "oddA": _parse_odd_text(str(row.get("oddA", ""))),
            "oddB": _parse_odd_text(str(row.get("oddB", ""))),
            "time": str(row.get("time", "")),
            "raw": str(row.get("raw", ""))[:300],
        })

    return clean_rows, " | ".join(audit)

def _flashscore_match_keys(name: str) -> List[str]:
    """
    Clés très tolérantes pour matcher ATP ↔ Flashscore.
    Exemples :
    - Yannick Hanfmann -> ["yannick hanfmann", "hanfmann"]
    - Alex de Minaur -> ["alex de minaur", "de minaur", "minaur"]
    - Giovanni Mpetshi Perricard -> ["giovanni mpetshi perricard", "mpetshi perricard", "perricard"]
    """
    tokens = _name_tokens(name)
    if not tokens:
        return []

    keys: List[str] = []
    keys.append(" ".join(tokens))

    if len(tokens) >= 2:
        keys.append(" ".join(tokens[-2:]))

    keys.append(tokens[-1])

    out: List[str] = []
    for key in sorted(keys, key=len, reverse=True):
        if key and len(key) >= 3 and key not in out:
            out.append(key)

    return out

def _contains_match_key(text: str, player_name: str) -> bool:
    norm = _norm_name(text)
    for key in _flashscore_match_keys(player_name):
        if re.search(rf"\b{re.escape(key)}\b", norm):
            return True
    return False

def _key_position(text: str, player_name: str) -> int:
    norm = _norm_name(text)
    positions: List[int] = []

    for key in _flashscore_match_keys(player_name):
        m = re.search(rf"\b{re.escape(key)}\b", norm)
        if m:
            positions.append(m.start())

    return min(positions) if positions else 10**9

def _find_flashscore_odds_for_match(player_a: str, player_b: str, rows: List[Dict[str, str]]) -> Dict[str, str]:
    for row in rows:
        fs_a = row.get("playerA", "")
        fs_b = row.get("playerB", "")
        raw = row.get("raw", "") or f"{fs_a} - {fs_b}"

        # Méthode normale.
        if _same_player_flashscore(player_a, fs_a) and _same_player_flashscore(player_b, fs_b):
            return {
                "oddA": row.get("oddA", ""),
                "oddB": row.get("oddB", ""),
                "sourcePlayerA": fs_a,
                "sourcePlayerB": fs_b,
                "orientation": "same",
            }

        if _same_player_flashscore(player_a, fs_b) and _same_player_flashscore(player_b, fs_a):
            return {
                "oddA": row.get("oddB", ""),
                "oddB": row.get("oddA", ""),
                "sourcePlayerA": fs_a,
                "sourcePlayerB": fs_b,
                "orientation": "reversed",
            }

        # Fallback très tolérant : Flashscore peut afficher nom + initiale ou juste nom de famille.
        a_in_fs_a = _contains_match_key(fs_a, player_a)
        b_in_fs_b = _contains_match_key(fs_b, player_b)

        if a_in_fs_a and b_in_fs_b:
            return {
                "oddA": row.get("oddA", ""),
                "oddB": row.get("oddB", ""),
                "sourcePlayerA": fs_a,
                "sourcePlayerB": fs_b,
                "orientation": "same_fallback",
            }

        a_in_fs_b = _contains_match_key(fs_b, player_a)
        b_in_fs_a = _contains_match_key(fs_a, player_b)

        if a_in_fs_b and b_in_fs_a:
            return {
                "oddA": row.get("oddB", ""),
                "oddB": row.get("oddA", ""),
                "sourcePlayerA": fs_a,
                "sourcePlayerB": fs_b,
                "orientation": "reversed_fallback",
            }

        # Dernier fallback : chercher les deux joueurs dans la ligne brute.
        if _contains_match_key(raw, player_a) and _contains_match_key(raw, player_b):
            pos_a = _key_position(raw, player_a)
            pos_b = _key_position(raw, player_b)

            if pos_a <= pos_b:
                return {
                    "oddA": row.get("oddA", ""),
                    "oddB": row.get("oddB", ""),
                    "sourcePlayerA": fs_a,
                    "sourcePlayerB": fs_b,
                    "orientation": "raw_same",
                }

            return {
                "oddA": row.get("oddB", ""),
                "oddB": row.get("oddA", ""),
                "sourcePlayerA": fs_a,
                "sourcePlayerB": fs_b,
                "orientation": "raw_reversed",
            }

    return {}

def _find_flashscore_odds_for_match_with_source_pair(match: Dict[str, Any], rows: List[Dict[str, str]]) -> Dict[str, str]:
    """
    Retrouve les cotes même après double-side.

    Priorité :
    1. chercher directement avec le Joueur A/Joueur B affichés ;
    2. si non trouvé, chercher avec la paire ATP source d'origine ;
    3. si trouvé avec la paire source, remettre oddA sur le Joueur A actuellement affiché.

    Exemple :
    - paire source ATP : Andrea Pellegrino vs Arthur Fils
    - affichage moteur : Arthur Fils vs Andrea Pellegrino
    - Flashscore trouve la source : Pellegrino/Fils = 8.20/1.08
    - on renvoie pour l'affichage : Fils = 1.08, Pellegrino = 8.20
    """
    player_a = str(match.get("playerA") or match.get("player_a") or "")
    player_b = str(match.get("playerB") or match.get("player_b") or "")

    found = _find_flashscore_odds_for_match(player_a, player_b, rows)
    if found:
        found["matchMethod"] = "display_pair"
        return found

    source_a = str(match.get("sourcePlayerA") or "")
    source_b = str(match.get("sourcePlayerB") or "")

    if not source_a or not source_b:
        return {}

    source_found = _find_flashscore_odds_for_match(source_a, source_b, rows)
    if not source_found:
        return {}

    source_odd_a = source_found.get("oddA", "")
    source_odd_b = source_found.get("oddB", "")

    # Si l'affichage actuel est le même que la source.
    if _same_player_flashscore(player_a, source_a) and _same_player_flashscore(player_b, source_b):
        return {
            "oddA": source_odd_a,
            "oddB": source_odd_b,
            "sourcePlayerA": source_found.get("sourcePlayerA", ""),
            "sourcePlayerB": source_found.get("sourcePlayerB", ""),
            "orientation": source_found.get("orientation", "same") + "_via_source_pair",
            "matchMethod": "source_pair_same",
        }

    # Si l'affichage actuel est inversé par rapport à la source.
    if _same_player_flashscore(player_a, source_b) and _same_player_flashscore(player_b, source_a):
        return {
            "oddA": source_odd_b,
            "oddB": source_odd_a,
            "sourcePlayerA": source_found.get("sourcePlayerA", ""),
            "sourcePlayerB": source_found.get("sourcePlayerB", ""),
            "orientation": source_found.get("orientation", "same") + "_via_source_pair_swapped",
            "matchMethod": "source_pair_swapped",
        }

    return {}

def enrich_result_with_flashscore_odds(result: Dict[str, Any], target_day: str) -> Dict[str, Any]:
    """
    Ajoute les cotes Flashscore après le calcul moteur.
    Le moteur reste inchangé et n'utilise jamais les cotes.
    """
    if not isinstance(result, dict):
        return result

    matches = result.get("matches")
    if not isinstance(matches, list) or not matches:
        return result

    try:
        flash_rows, flash_audit = fetch_flashscore_tennis_odds()
    except Exception as exc:
        flash_rows = []
        flash_audit = f"global_error={type(exc).__name__}: {exc}"

    matched_count = 0
    matched_sample: List[str] = []

    for match in matches:
        if not isinstance(match, dict):
            continue

        player_a = str(match.get("playerA") or match.get("player_a") or "")
        player_b = str(match.get("playerB") or match.get("player_b") or "")

        found = _find_flashscore_odds_for_match_with_source_pair(match, flash_rows)

        if found:
            odd_a = found.get("oddA", "")
            odd_b = found.get("oddB", "")

            match["oddA"] = odd_a
            match["oddB"] = odd_b
            match["playerAOdd"] = odd_a
            match["playerBOdd"] = odd_b
            match["player_a_odd"] = odd_a
            match["player_b_odd"] = odd_b
            match["coteA"] = odd_a
            match["coteB"] = odd_b
            match["oddsSource"] = "Flashscore"
            match["oddsStatus"] = "matched"
            match["oddsSourceMatch"] = f'{found.get("sourcePlayerA", "")} - {found.get("sourcePlayerB", "")}'
            matched_count += 1

            if len(matched_sample) < 8:
                matched_sample.append(f"{player_a} - {player_b} => {odd_a}/{odd_b} via {found.get('sourcePlayerA', '')} - {found.get('sourcePlayerB', '')} [{found.get('orientation', '')} | {found.get('matchMethod', '')}]")
        else:
            match.setdefault("oddA", "")
            match.setdefault("oddB", "")
            match.setdefault("playerAOdd", "")
            match.setdefault("playerBOdd", "")
            match.setdefault("player_a_odd", "")
            match.setdefault("player_b_odd", "")
            match.setdefault("coteA", "")
            match.setdefault("coteB", "")
            match["oddsSource"] = "Flashscore"
            match["oddsStatus"] = "not_found"

    result.setdefault("daily", {})
    result["daily"]["oddsSource"] = "Flashscore"
    result["daily"]["flashscoreRowsFound"] = len(flash_rows)
    result["daily"]["flashscoreMatched"] = matched_count
    result["daily"]["oddsRowsFound"] = len(flash_rows)
    result["daily"]["oddsMatched"] = matched_count
    result["daily"]["flashscoreAudit"] = (flash_audit + " | matched_sample=" + " || ".join(matched_sample))[-4000:]
    result["daily"]["oddsAudit"] = result["daily"]["flashscoreAudit"]

    return result

class FlashscoreOddsProvider:
    """
    Provider cotes Flashscore utilisé uniquement pour l'affichage Unity.
    Les cotes ne sont jamais utilisées par le moteur Tennis Motor.
    """

    def fetch_odds_audit(self) -> Dict[str, Any]:
        rows, audit_text = fetch_flashscore_tennis_odds()
        status = "ok" if rows else ("error" if "flashscore_error=" in audit_text or "playwright_import_error=" in audit_text else "empty")
        return {
            "provider": "flashscore",
            "status": status,
            "policy": "odds_display_only_engine_ignored",
            "url": FLASHSCORE_TENNIS_URL,
            "records": len(rows),
            "audit": str(audit_text)[-4000:],
        }

    def enrich_daily_response(self, daily_response: Dict[str, Any], *, target_day: str = "") -> Dict[str, Any]:
        """
        Enrichit daily_response par effet de bord, puis retourne un audit compatible app.py.
        """
        if not isinstance(daily_response, dict):
            return {
                "audit": {
                    "provider": "flashscore",
                    "status": "error",
                    "policy": "odds_display_only_engine_ignored",
                    "error": "daily_response_not_dict",
                    "targetDay": target_day,
                }
            }

        enriched = enrich_result_with_flashscore_odds(daily_response, target_day)
        daily = enriched.setdefault("daily", {})

        rows_found = int(daily.get("flashscoreRowsFound") or daily.get("oddsRowsFound") or 0)
        matched = int(daily.get("flashscoreMatched") or daily.get("oddsMatched") or 0)
        audit_text = str(daily.get("flashscoreAudit") or daily.get("oddsAudit") or "")

        status = "ok" if rows_found > 0 else ("error" if "flashscore_error=" in audit_text or "playwright_import_error=" in audit_text else "empty")

        audit = {
            "provider": "flashscore",
            "status": status,
            "policy": "odds_display_only_engine_ignored",
            "url": FLASHSCORE_TENNIS_URL,
            "records": rows_found,
            "matched": matched,
            "audit": audit_text[-4000:],
            "targetDay": target_day,
        }

        # Conserver aussi dans daily pour audit direct.
        daily["odds"] = audit
        return {"audit": audit}
