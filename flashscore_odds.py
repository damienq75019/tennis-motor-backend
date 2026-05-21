from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from typing import Any, Dict, List, Tuple

FLASHSCORE_TENNIS_URL = "https://www.flashscore.fr/tennis/"

def _paris_today() -> date:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Paris")).date()
    except Exception:
        return date.today()

def _target_day_offset(target_day: str) -> int:
    """
    Retourne l'écart en jours entre target_day et aujourd'hui Paris.
    Utilisé uniquement pour positionner le calendrier Flashscore.
    """
    value = (target_day or "").strip()
    if not value:
        return 0

    try:
        target = date.fromisoformat(value)
    except Exception:
        return 0

    offset = (target - _paris_today()).days

    # Sécurité : on ne clique jamais des dizaines de fois.
    if offset > 7:
        return 7
    if offset < -7:
        return -7
    return offset

def _flashscore_click_date_offset(page, target_day: str, audit: List[str]) -> None:
    """
    Positionne le calendrier Flashscore sur target_day.

    Pourquoi :
    - /daily?day=today scanne naturellement la bonne date.
    - /daily?day=tomorrow doit cliquer une fois sur le jour suivant.
    Sinon Flashscore retourne les cotes du jour courant et aucun match de demain ne matche.

    La fonction reste prudente : si le clic échoue, on continue sans bloquer le moteur.
    Les cotes restent affichage uniquement.
    """
    offset = _target_day_offset(target_day)
    audit.append(f"date_target={target_day or 'default'} offset={offset}")

    if offset == 0:
        return

    direction = "next" if offset > 0 else "prev"
    steps = abs(offset)

    next_selectors = [
        ".calendar__navigation--tomorrow",
        "button.calendar__navigation--tomorrow",
        "[class*='calendar__navigation--tomorrow']",
        "[aria-label*='jour suivant' i]",
        "[aria-label*='suivant' i]",
        "[aria-label*='next' i]",
        "[title*='jour suivant' i]",
        "[title*='suivant' i]",
        "[title*='next' i]",
    ]

    prev_selectors = [
        ".calendar__navigation--yesterday",
        "button.calendar__navigation--yesterday",
        "[class*='calendar__navigation--yesterday']",
        "[aria-label*='jour précédent' i]",
        "[aria-label*='précédent' i]",
        "[aria-label*='previous' i]",
        "[title*='jour précédent' i]",
        "[title*='précédent' i]",
        "[title*='previous' i]",
    ]

    selectors = next_selectors if direction == "next" else prev_selectors

    for step in range(steps):
        clicked = False

        for selector in selectors:
            try:
                locator = page.locator(selector)
                if locator.count() > 0:
                    locator.first.click(timeout=3500)
                    clicked = True
                    audit.append(f"date_click_step={step + 1}/{steps} direction={direction} selector={selector}")
                    break
            except Exception as exc:
                audit.append(f"date_click_selector_failed={selector}:{type(exc).__name__}")

        if not clicked:
            try:
                clicked = bool(page.evaluate(
                    """
                    (direction) => {
                        const all = Array.from(document.querySelectorAll('button, [role="button"], a, div'));
                        const isVisible = (el) => {
                            const r = el.getBoundingClientRect();
                            const s = window.getComputedStyle(el);
                            return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
                        };

                        const candidates = all.filter(el => {
                            if (!isVisible(el)) return false;
                            const cls = (el.className || '').toString().toLowerCase();
                            const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                            const title = (el.getAttribute('title') || '').toLowerCase();
                            const txt = (el.innerText || '').trim().toLowerCase();

                            if (direction === 'next') {
                                return cls.includes('tomorrow') || cls.includes('next') ||
                                       aria.includes('suivant') || aria.includes('next') ||
                                       title.includes('suivant') || title.includes('next') ||
                                       txt === '>' || txt === '›' || txt === '»';
                            }

                            return cls.includes('yesterday') || cls.includes('prev') || cls.includes('previous') ||
                                   aria.includes('précédent') || aria.includes('precedent') || aria.includes('previous') ||
                                   title.includes('précédent') || title.includes('precedent') || title.includes('previous') ||
                                   txt === '<' || txt === '‹' || txt === '«';
                        });

                        if (!candidates.length) return false;

                        // Flashscore place généralement les flèches de calendrier vers le centre-haut de la zone matches.
                        candidates.sort((a, b) => {
                            const ra = a.getBoundingClientRect();
                            const rb = b.getBoundingClientRect();
                            const scoreA = Math.abs(ra.top - 575) + Math.abs(ra.left - 920);
                            const scoreB = Math.abs(rb.top - 575) + Math.abs(rb.left - 920);
                            return scoreA - scoreB;
                        });

                        candidates[0].click();
                        return true;
                    }
                    """,
                    direction,
                ))
                if clicked:
                    audit.append(f"date_click_step={step + 1}/{steps} direction={direction} selector=js_fallback")
            except Exception as exc:
                audit.append(f"date_click_js_failed={type(exc).__name__}: {exc}")

        if not clicked:
            audit.append(f"date_click_failed_step={step + 1}/{steps} direction={direction}")
            return

        try:
            page.wait_for_timeout(2500)
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass

    # Audit lisible de la date visible après clic, sans bloquer.
    try:
        body_text = page.locator("body").inner_text(timeout=8000)
        m = re.search(r"\b\d{1,2}/\d{2}\s+[A-ZÉ]{2,3}\b", body_text)
        if m:
            audit.append(f"date_visible_after_click={m.group(0)}")
    except Exception:
        pass



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


def _split_flashscore_short_name(name: str) -> Tuple[List[str], List[str]]:
    """
    Sépare un nom court Flashscore.

    Exemples :
    - "Svrcina D." -> (["svrcina"], ["d"])
    - "Prado Angelo J. C." -> (["prado", "angelo"], ["j", "c"])
    - "de Minaur A." -> (["de", "minaur"], ["a"])
    - "Blanch Dar." -> (["blanch"], ["dar"])

    Flashscore n'utilise pas toujours une seule lettre pour le prénom.
    Exemple vu en production :
    - Sportradar : "Blanch, Darwin"
    - Flashscore : "Blanch Dar."
    On traite donc aussi les petits suffixes alphabétiques de 2-4 lettres
    comme des abréviations de prénom.
    """
    tokens = _flashscore_tokens_keep_initials(name)
    initials: List[str] = []

    while tokens:
        last = tokens[-1]

        # Initiale classique : "D.", "P.", "H.".
        if len(last) == 1:
            initials.insert(0, last)
            tokens = tokens[:-1]
            continue

        # Abréviation courte du prénom : "Dar." pour Darwin.
        # On ne le fait que s'il reste au moins un token pour le nom de famille.
        if len(tokens) >= 2 and 2 <= len(last) <= 4:
            initials.insert(0, last)
            tokens = tokens[:-1]
            continue

        break

    return tokens, initials


def _full_name_profile(name: str) -> Dict[str, Any]:
    """
    Profil robuste pour comparer Sportradar ↔ Flashscore.

    Sportradar renvoie très souvent :
    - "Svrcina, Dalibor"
    - "Prado Angelo, Juan Carlos"
    - "de Minaur, Alex"

    Flashscore renvoie plutôt :
    - "Svrcina D."
    - "Prado Angelo J. C."
    - "de Minaur A."

    L'ancien matching lisait "Svrcina, Dalibor" comme si "Dalibor" était le nom
    de famille. C'est ce qui donnait 0 match alors que Flashscore avait bien
    les lignes et les cotes.
    """
    raw = (name or "").strip()

    if "," in raw:
        left, right = raw.split(",", 1)
        surname_tokens = _flashscore_tokens_keep_initials(left)
        given_tokens = _flashscore_tokens_keep_initials(right)
        ordered_tokens = given_tokens + surname_tokens
        original_tokens = surname_tokens + given_tokens
    else:
        original_tokens = _flashscore_tokens_keep_initials(raw)
        ordered_tokens = list(original_tokens)
        if len(original_tokens) >= 2:
            given_tokens = original_tokens[:-1]
            surname_tokens = [original_tokens[-1]]
        else:
            given_tokens = []
            surname_tokens = list(original_tokens)

    surname_candidates: List[str] = []

    def add_candidate(tokens: List[str]) -> None:
        txt = " ".join([t for t in tokens if t]).strip()
        if txt and txt not in surname_candidates:
            surname_candidates.append(txt)

    if surname_tokens:
        # Nom complet à gauche de la virgule : "prado angelo", "de minaur".
        add_candidate(surname_tokens)
        # Suffixes utiles : "angelo", "minaur", "carabelli".
        for size in range(1, min(4, len(surname_tokens)) + 1):
            add_candidate(surname_tokens[-size:])

    if not surname_candidates and ordered_tokens:
        for size in range(1, min(4, len(ordered_tokens)) + 1):
            add_candidate(ordered_tokens[-size:])

    given_initials = [t[0] for t in given_tokens if t]
    first_initial = given_initials[0] if given_initials else (ordered_tokens[0][0] if ordered_tokens else "")

    full_variants: List[str] = []
    for tokens in (ordered_tokens, original_tokens):
        txt = " ".join(tokens).strip()
        if txt and txt not in full_variants:
            full_variants.append(txt)

    return {
        "ordered_tokens": ordered_tokens,
        "original_tokens": original_tokens,
        "given_tokens": given_tokens,
        "surname_tokens": surname_tokens,
        "surname_candidates": surname_candidates,
        "given_initials": given_initials,
        "first_initial": first_initial,
        "full_variants": full_variants,
    }


def _initials_match(flash_initials: List[str], profile: Dict[str, Any]) -> bool:
    if not flash_initials:
        return True

    given_tokens = list(profile.get("given_tokens") or [])
    given_initials = list(profile.get("given_initials") or [])
    first_initial = str(profile.get("first_initial") or "")

    if not given_tokens and not given_initials:
        return bool(first_initial and flash_initials[0][0] == first_initial)

    def part_matches(part: str, given_token: str) -> bool:
        part = (part or "").strip()
        given_token = (given_token or "").strip()

        if not part or not given_token:
            return False

        # Cas classique : "D." ↔ Dalibor.
        if len(part) == 1:
            return part == given_token[0]

        # Cas Flashscore : "Dar." ↔ Darwin.
        return given_token.startswith(part) or part.startswith(given_token)

    # "D." ↔ Dalibor ; "J. C." ↔ Juan Carlos ; "Dar." ↔ Darwin.
    if given_tokens and len(flash_initials) <= len(given_tokens):
        ok = True
        for index, part in enumerate(flash_initials):
            if not part_matches(part, given_tokens[index]):
                ok = False
                break
        if ok:
            return True

    # Ancien comportement conservé pour les initiales simples.
    if given_initials:
        if [p[0] for p in flash_initials] == given_initials[:len(flash_initials)]:
            return True

        if len(flash_initials) == 1 and flash_initials[0][0] == given_initials[0]:
            return True

    return False


def _same_player_flashscore(full_name: str, flash_name: str) -> bool:
    """
    Matching robuste ATP full name <-> Flashscore.

    Corrige le bug vu en production step2.7 :
    Flashscore trouvait bien des lignes comme "Svrcina D. - Faurel T. = 1.23/4.00",
    mais le backend ne matchait rien contre "Svrcina, Dalibor" parce que le format
    Sportradar est souvent "Nom, Prénom".
    """
    if _same_player(full_name, flash_name):
        return True

    full = _full_name_profile(full_name)
    flash_tokens = _flashscore_tokens_keep_initials(flash_name)

    if not full.get("ordered_tokens") or not flash_tokens:
        return False

    full_variants = set(full.get("full_variants") or [])
    flash_join = " ".join(flash_tokens)

    if flash_join in full_variants:
        return True

    flash_surname_tokens, flash_initials = _split_flashscore_short_name(flash_name)
    flash_surname = " ".join(flash_surname_tokens).strip()

    surname_candidates = set(full.get("surname_candidates") or [])

    # Cas principal Flashscore : "Svrcina D." ↔ "Svrcina, Dalibor".
    if flash_surname and flash_initials:
        if flash_surname in surname_candidates and _initials_match(flash_initials, full):
            return True

        # Tolérance nom composé : tous les morceaux du nom Flashscore sont contenus
        # dans le nom de famille Sportradar et les initiales correspondent.
        for candidate in surname_candidates:
            cand_tokens = candidate.split()
            if flash_surname_tokens and all(tok in cand_tokens for tok in flash_surname_tokens):
                if _initials_match(flash_initials, full):
                    return True

    # Cas nom de famille seul ou nom composé sans initiale.
    if flash_surname and not flash_initials:
        if flash_surname in surname_candidates:
            return True
        if len(flash_surname_tokens) == 1 and flash_surname_tokens[0] in surname_candidates:
            return True

    # Dernière tolérance : token du nom de famille suffisamment long.
    if len(flash_tokens) == 1 and len(flash_tokens[0]) >= 4:
        if flash_tokens[0] in surname_candidates:
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

def fetch_flashscore_tennis_odds(target_day: str = "") -> Tuple[List[Dict[str, str]], str]:
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

            # Step 2.7.2 :
            # positionner Flashscore sur target_day avant de scanner les onglets.
            _flashscore_click_date_offset(page, target_day, audit)

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
        flash_rows, flash_audit = fetch_flashscore_tennis_odds(target_day)
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
