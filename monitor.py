"""Monitor de ingressos BTS WORLD TOUR ARIRANG - São Paulo (28, 30 e 31/10/2026).

Uma passada por execução (agendar a cada 5 min) ou --loop pra rodar direto.
Fontes: Ticketmaster (ingressos voltando ao site), Bluesky (anúncios de fãs e
feed da BuyTicket), Google News (notícias sobre transferência/liberação).
"""
import argparse
import base64
import html
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
import traceback
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

import requests

BASE = Path(__file__).resolve().parent
CONFIG = json.loads((BASE / "config.json").read_text(encoding="utf-8-sig"))  # -sig: aceita BOM do Notepad
if os.environ.get("NTFY_TOPICO"):  # GitHub Actions: tópico vem de segredo, fora do repositório público
    CONFIG["notificacao"]["ntfy_topico"] = os.environ["NTFY_TOPICO"]
PAINEL_URL = os.environ.get("PAINEL_URL")  # link do painel online, usado no clique do resumo diário
STATE_FILE = BASE / "state.json"
LOG_FILE = BASE / "alertas.log"
PAINEL_FILE = BASE / "painel.html"

BRT = timezone(timedelta(hours=-3))
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept-Language": "pt-BR,pt;q=0.9",
}
UA_REDDIT = {"User-Agent": "windows:arirang-monitor:v1.0 (monitor pessoal de ingressos)"}
TIMEOUT = 25


def agora():
    return datetime.now(BRT)


def log(msg):
    linha = f"[{agora():%d/%m %H:%M:%S}] {msg}"
    print(linha)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(linha + "\n")


def carregar_estado():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def salvar_estado(estado):
    STATE_FILE.write_text(json.dumps(estado, ensure_ascii=False, indent=1), encoding="utf-8")


def sem_acento(texto):
    return "".join(c for c in unicodedata.normalize("NFD", texto.lower())
                   if unicodedata.category(c) != "Mn")


# ---------------------------------------------------------------- notificações

SILENCIOSO = False


def notificar(titulo, mensagem, url=None, urgente=False, ticketmaster=False):
    log(f"ALERTA: {titulo} | {mensagem} | {url or ''}")
    if SILENCIOSO:
        return
    cfg = dict(CONFIG["notificacao"])
    if cfg.get("ntfy_so_ticketmaster") and not ticketmaster:
        cfg["ntfy_topico"] = ""  # notebook: celular recebe o resto pela nuvem (GitHub Actions), sem duplicar
    if cfg.get("windows") and os.name == "nt":
        try:
            toast_windows(titulo, mensagem, url, urgente)
        except Exception as e:
            log(f"erro toast: {e}")
    if cfg.get("ntfy_topico"):
        try:
            payload = {
                "topic": cfg["ntfy_topico"],
                "title": titulo,
                "message": mensagem,
                "priority": 5 if urgente else 3,
                "tags": ["rotating_light" if urgente else "ticket"],
            }
            if url and url.startswith("http"):  # file:// do painel não abre no celular
                payload["click"] = url
            requests.post("https://ntfy.sh/", json=payload, timeout=TIMEOUT)
        except Exception as e:
            log(f"erro ntfy: {e}")


def toast_windows(titulo, mensagem, url, urgente):
    esc = lambda s: html.escape(s or "", quote=True)
    launch = f' activationType="protocol" launch="{esc(url)}"' if url else ""
    som = "Notification.Looping.Alarm" if urgente else "Notification.Default"
    xml = (f'<toast{launch} scenario="{"reminder" if urgente else "default"}">'
           f'<visual><binding template="ToastGeneric"><text>{esc(titulo)}</text>'
           f'<text>{esc(mensagem)}</text></binding></visual>'
           f'<audio src="ms-winsoundevent:{som}" loop="false"/></toast>')
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null;"
        "$x = New-Object Windows.Data.Xml.Dom.XmlDocument;"
        f"$x.LoadXml([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{base64.b64encode(xml.encode()).decode()}')));"
        "$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($x))"
    )
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand",
         base64.b64encode(ps.encode("utf-16-le")).decode()],
        check=True, capture_output=True, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


# ---------------------------------------------------------------- Ticketmaster

def checar_ticketmaster(estado):
    tm = estado.setdefault("ticketmaster", {})
    primeira_vez = not tm
    cfg = CONFIG["ticketmaster"]

    for data, url in cfg["datas"].items():
        try:
            r = requests.get(url, headers=UA, timeout=TIMEOUT)
            pagina = r.content.decode("utf-8", "replace")
            m = re.search(r'"available":(true|false),"sectors":(\[\])?', pagina)
            if r.status_code >= 400 or len(pagina) < 2000:
                status = "erro"  # bloqueio (ex: IP de servidor), não é mudança de página
            elif not m:
                status = "desconhecido"
            elif m.group(1) == "true" or m.group(2) is None:
                status = "disponivel"
            else:
                status = "esgotado"
        except requests.RequestException as e:
            status = "erro"
            log(f"Ticketmaster {data}: {e}")

        anterior = tm.get(data, {})
        seguidos = anterior.get("estranho_seguidos", 0) + 1 if status == "desconhecido" else 0
        tm[data] = {"status": status, "checado": agora().isoformat(), "estranho_seguidos": seguidos}
        if status == "disponivel" and anterior.get("status") != "disponivel":
            notificar(f"INGRESSO NO TICKETMASTER {data}!",
                      f"Show {data} apareceu com setores à venda. Corre, preço oficial.",
                      url, urgente=True, ticketmaster=True)
        elif status == "desconhecido":
            # página diferente do normal: fila virtual, manutenção ou layout novo
            (BASE / f"debug_tm_{data.replace('/', '-')}.html").write_text(pagina, encoding="utf-8")
            if seguidos == 1:
                titulo = re.search(r"<title[^>]*>(.*?)</title>", pagina, re.S | re.I)
                log(f"Ticketmaster {data}: página diferente do normal ({len(pagina)} bytes, título: "
                    f"{titulo.group(1).strip()[:80] if titulo else '-'}) - salva em debug_tm_*.html")
            elif seguidos == 3:
                notificar(f"Ticketmaster {data}: página mudou há 15 min",
                          "Pode ser fila virtual (liberação?) ou layout novo. Confere no site.", url, ticketmaster=True)

    # Página principal: bolinhas de status por data + links novos (datas extras, revenda oficial)
    try:
        pagina = requests.get(cfg["principal"], headers=UA, timeout=TIMEOUT).content.decode("utf-8", "replace")
        disp = len(re.findall(r'<span class="tmpe-status-dot tmpe-dot-available"', pagina))
        links = sorted(set(
            "https://www.ticketmaster.com.br" + l if l.startswith("/") else l
            for l in re.findall(r'href="((?:https://www\.ticketmaster\.com\.br)?/event/[^"?#]+)', pagina)
            if "bts" in l.lower()
        ))
        conhecidos = set(tm.get("links", []))
        novos = [l for l in links if l not in conhecidos] if conhecidos else []
        if disp and not tm.get("principal_disponivel"):
            notificar("TICKETMASTER: data BTS disponível!",
                      f"{disp} data(s) saíram de 'Esgotado' na página principal.",
                      cfg["principal"], urgente=True, ticketmaster=True)
        for l in novos:
            notificar("Ticketmaster: página nova do BTS", l, l, urgente=True, ticketmaster=True)
        tm["principal_disponivel"] = disp
        tm["links"] = sorted(conhecidos | set(links))
    except requests.RequestException as e:
        log(f"Ticketmaster principal: {e}")

    if primeira_vez:
        log("Ticketmaster: estado inicial " +
            ", ".join(f"{d}={tm[d]['status']}" for d in cfg["datas"]))


# ---------------------------------------------------------------- anúncios (Bluesky, X, Reddit)

# "vendo" também é gerúndio de ver ("to vendo o show"), então exige objeto de ingresso logo depois
OBJETO = r"(?:\s+(?:o|os|meu|meus|minha|minhas|um|uma|dois|duas|\d|mais))?(?:\s+\d)?\s+(?:ingresso|ingressos|ingressinho|entrada|pista|arquibancada|cadeira|lugar)"
RE_VENDA = re.compile(
    r"(?<!to )(?<!tava )(?<!estou )(?<!estava )(?<!fico )(?<!ficar )(?<!ando )(?<!vivo )"
    r"\b(?:vendo|repasso|passo|vende-se|desapego)" + OBJETO +
    r"|\b(?:to|estou|tamo|estamos) vendendo" + OBJETO +
    r"|\brepasse\b|\bpreco de custo\b|\bvalor pago\b|\bpelo valor que paguei\b|\bdesapegando\b"
    r"|\b(?:selling|wts|for sale)\b")
RE_COMPRA = re.compile(r"\b(?:compro|procuro|procurando|quero comprar|busco|preciso de|"  # \b no fim: "comprovo" não é "compro"
                       r"nao tem nenhuma|mentira|wtb|looking for|buying)\b"
                       r"|\balguem (vend|tem|ta vend|esta vend|estiver vend|com)|\bquem (vende|tiver|tem)")
RE_LOCAL = re.compile(r"ingress|sao paulo|morumbi|brasil|brazil|\bsp\b|quentro|28/10|30/10|31/10|"
                      r"arquibancada|cadeira (superior|inferior)|meia")
RE_OUTRA_CIDADE = re.compile(r"santiago|chile|lima\b|peru|bogota|colombia|buenos aires|argentina|"
                             r"mexico|cdmx|monterrey|guadalajara")
RE_PRECO = re.compile(r"r\$\s*(\d{1,3}(?:\.\d{3})+|\d+)(?:,\d{1,2})?"
                      r"|(\d{3,5})\s*(?:reais|conto|dinheiros|pila)"
                      r"|(?:por|valor|preco|sai a|sai por|apenas|so)\s*:?\s*(\d{3,5})\b")
DIAS_MAX_ANUNCIO = 30
SETORES = ["cadeira superior", "cadeira inferior", "arquibancada", "pista", "soundcheck", "vip"]


def extrair_precos(texto):
    precos = []
    for grupos in RE_PRECO.findall(texto):
        valor = int(next(g for g in grupos if g).replace(".", ""))
        if 50 <= valor <= 50000:
            precos.append(valor)
    return precos


def classificar(texto):
    t = sem_acento(texto)
    setor = next((s for s in SETORES if s in t), None)
    if setor == "soundcheck":
        setor = "vip"
    categoria = "meia" if "meia" in t else ("inteira" if "inteira" in t else None)
    datas = [d for d in CONFIG["datas"] if d in t or f"dia {d[:2]}" in t]
    return setor, categoria, datas


def preco_face(setor, categoria):
    """Valor oficial com taxa de 20% (ou None se setor desconhecido)."""
    face = CONFIG["precos_face"].get(setor or "")
    if not face:
        return None
    base = face[1] if categoria == "meia" else face[0]
    return round(base * (1 + CONFIG["taxa_servico"]))


def analisar_texto(texto):
    """Classifica texto de um post. None se não é sobre show BTS no Brasil."""
    t = sem_acento(texto)
    if not re.search(r"\b(bts|arirang)\b", t):
        return None
    if "vendo por: r$" in t:  # anúncio automático da BuyTicket (Bluesky e X)
        m = re.search(r"Vendo por: R\$\s*([\d.]+)", texto)
        tipo = re.search(r"Tipo: (.+)", texto)
        cat = re.search(r"Categoria: (.+)", texto)
        data = re.search(r"\((\d\d/\d\d)/2026\)", texto)
        categoria_txt = cat.group(1).strip() if cat else ""
        return {
            "buyticket": True,
            "vendedor": True,
            "preco": int(m.group(1).replace(".", "")) if m else None,
            "setor": sem_acento(tipo.group(1).strip()) if tipo else None,
            "categoria": "meia" if "meia" in sem_acento(categoria_txt) else "inteira",
            "categoria_txt": categoria_txt,
            "datas": [data.group(1)] if data else [],
        }
    if not RE_LOCAL.search(t):  # evita anúncio de show em outro país
        return None
    if RE_OUTRA_CIDADE.search(t) and not re.search(r"sao paulo|morumbi", t):
        return None
    setor, categoria, datas = classificar(texto)
    precos = extrair_precos(t)
    return {
        "buyticket": False,
        "vendedor": bool(RE_VENDA.search(t)) and not RE_COMPRA.search(t),
        "preco": min(precos) if precos else None,
        "setor": setor,
        "categoria": categoria,
        "categoria_txt": categoria or "",
        "datas": datas,
    }


def avaliar(anuncio):
    """Retorna (alertar: bool, selo: str)."""
    if anuncio["setor"] == "vip":
        return False, "VIP não transfere - golpe provável"
    if anuncio["categoria"] == "meia" and not CONFIG["tenho_meia"]:
        return False, "meia (sem direito)"
    preco = anuncio["preco"]
    if preco is None:
        return CONFIG["notificacao"]["alertar_anuncio_sem_preco"], "sem preço"
    face = preco_face(anuncio["setor"], anuncio["categoria"])
    if preco < 150:
        return False, "preço irreal - golpe provável"
    if face and preco < face * 0.6:
        return True, "muito abaixo do oficial - desconfie"
    if face and preco <= face:
        return True, "preço de face"
    if preco <= CONFIG["teto_preco"]:
        return True, "dentro do teto"
    return False, "acima do teto"


# Frases dos roteiros de golpe vistos no X em 30/09 (redes "gastei no sound" e "dispenso golpista")
RE_ROTEIRO = [re.compile(p) for p in (
    r"gastei (muito|mt|mto|horrores|demais) no (sound|dia \d\d)",
    r"vou de sound ?check",
    r"comprov\w* tudo",
    r"comprov\w* .{0,40}nos meus dados",
    r"feedback de outra venda",
    r"dispens\w* golpista",
    r"transferencia (do ingresso )?imediat",
    r"\btags ?[:;]",
    r"troco identidade",
    r"primeira vez vendendo",
    r"(passo|passar|mandar|mando|enviar|envio) (os )?meus (dados|documentos)",
    r"sou army e de confianca",
)]
LIMIAR_COPIA = 0.45  # Jaccard de trigramas de palavras


def _normalizar(texto):
    t = re.sub(r"https?://\S+|@\w+", " ", sem_acento(texto))
    return " ".join(re.sub(r"[^a-z ]+", " ", t).split())  # sem números: pega modelo com preço/data trocados


def _trigramas(t):
    w = t.split()
    return {" ".join(w[i:i + 3]) for i in range(len(w) - 2)}


def sinais_golpe(soc, texto, autor):
    """Texto quase igual ao de outra conta ou 2+ frases de roteiro. Retorna selo ou None."""
    tri = _trigramas(_normalizar(texto))
    for imp in soc.get("impressoes", []):
        if imp["autor"] == autor:
            continue
        outro = _trigramas(imp["texto"])
        if tri and outro and len(tri & outro) / len(tri | outro) >= LIMIAR_COPIA:
            for a in soc.get("anuncios", []):  # marca também o anúncio antigo da outra conta
                if a["autor"] == imp["autor"] and "golpe" not in a["selo"]:
                    a["selo"] = f"texto copiado de @{autor} - golpe provável"
            return f"texto copiado de @{imp['autor']} - golpe provável"
    frases = sum(1 for r in RE_ROTEIRO if r.search(sem_acento(texto)))
    if frases >= 2:
        return f"roteiro de golpe ({frases} frases) - evite"
    return None


def processar_posts(estado, posts, fonte):
    """posts: dicts com id, fonte, texto, autor, url, criado (datetime com fuso)."""
    soc = estado.setdefault("social", {"vistos": [], "anuncios": []})
    iniciadas = soc.setdefault("fontes_iniciadas", ["Bluesky"] if soc["vistos"] else [])
    primeira_vez = fonte not in iniciadas
    vistos = set(soc["vistos"])
    limite = agora() - timedelta(hours=24 if primeira_vez else 72)

    novos = 0
    for p in sorted(posts, key=lambda p: p["criado"]):
        if p["id"] in vistos:
            continue
        vistos.add(p["id"])
        if p["criado"] < agora() - timedelta(days=DIAS_MAX_ANUNCIO):
            continue
        a = analisar_texto(p["texto"])
        if not a or not a["vendedor"]:
            continue
        alertar, selo = avaliar(a)
        buyticket = a.pop("buyticket")
        if not buyticket:  # posts da BuyTicket são modelo automático, não comparar
            suspeita = sinais_golpe(soc, p["texto"], p["autor"])
            if suspeita:
                alertar, selo = False, suspeita
            soc.setdefault("impressoes", []).append(
                {"autor": p["autor"], "texto": _normalizar(p["texto"])[:600], "criado": p["criado"].isoformat()})
            soc["impressoes"] = soc["impressoes"][-800:]
        a.update({
            "fonte": ("BuyTicket" if p["fonte"] == "Bluesky" else f"BuyTicket/{p['fonte']}")
                     if buyticket else p["fonte"],
            "url": p["url"],
            "criado": p["criado"].astimezone(BRT).isoformat(),
            "texto": p["texto"][:280],
            "autor": p["autor"],
            "selo": selo,
        })
        soc["anuncios"].append(a)
        novos += 1
        if alertar and p["criado"] >= limite:
            preco = f"R$ {a['preco']}" if a["preco"] else "preço não informado"
            detalhes = " / ".join(x for x in [a["setor"], a["categoria_txt"], ",".join(a["datas"])] if x)
            notificar(f"Anúncio {a['fonte']}: {preco} ({selo})",
                      f"{detalhes or 'setor ?'} - @{a['autor']}: {a['texto'][:120]}",
                      a["url"], urgente=(selo == "preço de face"))

    if primeira_vez:
        iniciadas.append(fonte)
    soc["vistos"] = list(vistos)[-5000:]
    soc["anuncios"] = soc["anuncios"][-500:]
    if novos:
        log(f"{fonte}: {novos} anúncio(s) novo(s) de venda")


def checar_bluesky(estado):
    brutos = {}
    for q in CONFIG["buscas_bluesky"]:
        try:
            r = requests.get("https://api.bsky.app/xrpc/app.bsky.feed.searchPosts",
                             params={"q": q, "limit": 50, "sort": "latest", "lang": "pt"},
                             headers=UA, timeout=TIMEOUT)
            for p in r.json().get("posts", []):
                brutos[p["uri"]] = p
        except Exception as e:
            log(f"Bluesky '{q}': {e}")
    try:
        r = requests.get("https://api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed",
                         params={"actor": "buyticket.bsky.social", "limit": 50},
                         headers=UA, timeout=TIMEOUT)
        for f in r.json().get("feed", []):
            brutos[f["post"]["uri"]] = f["post"]
    except Exception as e:
        log(f"Bluesky BuyTicket: {e}")

    posts = []
    for uri, p in brutos.items():
        rec = p["record"]
        autor = p["author"]["handle"]
        link_anuncio = rec.get("embed", {}).get("external", {}).get("uri")
        posts.append({
            "id": uri,
            "fonte": "Bluesky",
            "texto": rec.get("text", ""),
            "autor": autor,
            "url": link_anuncio if autor == "buyticket.bsky.social" and link_anuncio
                   else f"https://bsky.app/profile/{autor}/post/{uri.rsplit('/', 1)[-1]}",
            "criado": datetime.fromisoformat(rec["createdAt"].replace("Z", "+00:00")),
        })
    processar_posts(estado, posts, "Bluesky")


BT_API = "https://buyticketbrasil.com/api/backend/marketplaces"


def checar_buyticket(estado):
    """Menor preço por data/setor/categoria direto da API do site (a mesma que a página usa)."""
    bt = estado.setdefault("buyticket", {"precos": {}, "alertados": []})
    cfg = CONFIG["buyticket"]
    get = lambda url, **p: requests.get(url, params=p or None, headers={**UA, "Accept": "application/json"},
                                        timeout=TIMEOUT).json()
    sessoes = get(f"{BT_API}/sessions", EventSlug=cfg["slug"], OnlyUpcoming="true",
                  PageNumber=1, PageSize=50)["data"]
    precos = {}
    for s in sessoes:
        if "camarote" in sem_acento(s["venue"]["name"]):
            continue
        data = datetime.fromisoformat(s["dates"][0].replace("Z", "+00:00")).astimezone(BRT).strftime("%d/%m")
        try:
            _precos_sessao(s, data, get, precos, bt, cfg)
        except (requests.RequestException, ValueError, KeyError) as erro:
            # site instável às vezes responde HTML: mantém os preços anteriores desta data
            log(f"BuyTicket {data}: resposta inválida ({type(erro).__name__}), mantendo preços anteriores")
            precos.update({k: v for k, v in bt["precos"].items() if k.startswith(data + "|")})
    bt["precos"] = precos
    bt["alertados"] = bt["alertados"][-500:]


def _precos_sessao(s, data, get, precos, bt, cfg):
    """Menor preço por setor/categoria de uma data; alerta o que estiver dentro do teto."""
    url = f"https://buyticketbrasil.com/event/{cfg['slug']}/session/{s['id']}"
    for tk in get(f"{BT_API}/sessions/{s['id']}/tickets")["tickets"]:
        if "camarote" in sem_acento(tk["ticketType"]):
            continue
        for c in get(f"{BT_API}/sessions/{s['id']}/tickets/{tk['ticketId']}/categories")["categories"]:
            valor = c["amount"]["value"]
            if not valor or not c.get("listingId"):
                continue  # categoria sem anúncio
            precos[f"{data}|{tk['ticketType']}|{c['name']}"] = {"valor": valor, "url": url}
            if (c["name"] in cfg["categorias_aceitas"] and valor <= CONFIG["teto_preco"]
                    and c["listingId"] not in bt["alertados"]):
                notificar(f"BuyTicket: R$ {valor:.0f} {tk['ticketType']} {c['name']} {data}",
                          "Dentro do teto. Pague só pela plataforma (dinheiro fica retido até a entrega).",
                          url, urgente=True)
                bt["alertados"].append(c["listingId"])
        time.sleep(0.2)


def checar_reddit(estado):
    ns = {"a": "http://www.w3.org/2005/Atom"}
    posts = []
    for i, q in enumerate(CONFIG["buscas_reddit"]):
        if i:
            time.sleep(8)  # Reddit sem login limita rápido
        r = requests.get("https://www.reddit.com/search.rss",
                         params={"q": q, "sort": "new", "limit": 50},
                         headers=UA_REDDIT, timeout=TIMEOUT)
        if r.status_code in (403, 429):  # 403: Reddit bloqueia IP de servidor (GitHub Actions)
            log(f"Reddit: acesso negado ({r.status_code}), tenta na próxima rodada")
            break
        r.raise_for_status()
        for e in ET.fromstring(r.content).findall("a:entry", ns):
            corpo = re.sub(r"<[^>]+>", " ", html.unescape(e.findtext("a:content", "", ns)))
            corpo = corpo.split("submitted by")[0]
            posts.append({
                "id": "reddit:" + e.findtext("a:id", "", ns),
                "fonte": "Reddit",
                "texto": e.findtext("a:title", "", ns) + "\n" + corpo,
                "autor": e.findtext("a:author/a:name", "", ns).replace("/u/", ""),
                "url": e.find("a:link", ns).get("href"),
                "criado": datetime.fromisoformat(e.findtext("a:published", "", ns)
                                                 or e.findtext("a:updated", "", ns)),
            })
    processar_posts(estado, posts, "Reddit")


def checar_x(estado):
    xs = estado.setdefault("x", {})
    if not CONFIG.get("x_ativo"):  # X exibiu verificação anti-bot em 30/09; não insistir
        return
    try:
        import fonte_x
    except ImportError:
        log("X: playwright não instalado")
        return
    try:
        posts = fonte_x.buscar(CONFIG["buscas_x"], headless=CONFIG.get("x_headless", True))
    except fonte_x.PrecisaLogin:
        if not xs.get("precisa_login"):
            notificar("Monitor BTS: login do X expirou",
                      "Rode: python monitor.py --login-x (busca no X parada até lá)")
        xs["precisa_login"] = True
        return
    xs["precisa_login"] = False
    xs["ultimo_total"] = len(posts)
    processar_posts(estado, posts, "X")


# ---------------------------------------------------------------- notícias

RE_NOTICIA = re.compile(r"ingress|transfer|quentro|revenda|lote|liberad|extra|cambis|golpe|ticketmaster")
RE_RUIDO = re.compile(r"live viewing|filme|cinema|trailer|elenco")


def checar_noticias(estado):
    nts = estado.setdefault("noticias", {"vistos": [], "itens": []})
    primeira_vez = not nts["vistos"]
    vistos = set(nts["vistos"])
    for q in CONFIG["buscas_noticias"]:
        url = ("https://news.google.com/rss/search?q=" + quote(q + " when:7d") +
               "&hl=pt-BR&gl=BR&ceid=BR:pt-419")
        try:
            raiz = ET.fromstring(requests.get(url, headers=UA, timeout=TIMEOUT).content)
        except Exception as e:
            log(f"Notícias '{q}': {e}")
            continue
        for item in raiz.iter("item"):
            titulo = item.findtext("title") or ""
            link = item.findtext("link") or ""
            if link in vistos:
                continue
            vistos.add(link)
            t = sem_acento(titulo.rsplit(" - ", 1)[0])  # tira nome do veículo ("- Ingresso.com")
            if "bts" not in t or not RE_NOTICIA.search(t) or RE_RUIDO.search(t):
                continue
            try:
                data = parsedate_to_datetime(item.findtext("pubDate")).astimezone(BRT).isoformat()
            except Exception:
                data = agora().isoformat()
            nts["itens"].append({"titulo": titulo, "link": link, "data": data})
            if not primeira_vez:
                notificar("Notícia BTS ingressos", titulo, link)
    nts["vistos"] = list(vistos)[-2000:]
    nts["itens"] = sorted(nts["itens"], key=lambda i: i["data"])[-60:]


# ---------------------------------------------------------------- resumo e painel

def resumo_diario(estado):
    hoje = agora().strftime("%Y-%m-%d")
    if agora().hour < CONFIG["notificacao"]["hora_resumo_diario"] or estado.get("ultimo_resumo") == hoje:
        return
    estado["ultimo_resumo"] = hoje
    desde = (agora() - timedelta(hours=24)).isoformat()
    ultimos = [a for a in estado.get("social", {}).get("anuncios", []) if a["criado"] >= desde]
    com_preco = [a for a in ultimos if a["preco"] and a["selo"] not in ("preço irreal - golpe provável",)]
    barato = min(com_preco, key=lambda a: a["preco"]) if com_preco else None
    tm = estado.get("ticketmaster", {})
    status = ", ".join(f"{d}: {tm.get(d, {}).get('status', '?')}" for d in CONFIG["datas"])
    msg = f"24h: {len(ultimos)} anúncios de venda. Ticketmaster {status}."
    if barato:
        msg += f" Mais barato: R$ {barato['preco']} {barato['setor'] or ''} ({barato['fonte']})."
    notificar("Resumo diário BTS", msg, PAINEL_URL or PAINEL_FILE.as_uri())


def brl(valor):
    return "R$ " + f"{valor:,.0f}".replace(",", ".")


PAINEL_CSS = """
:root{--bg:#0f0a1e;--bg2:#1a1033;--card:#211640;--card2:#2a1c52;--fg:#f3eefe;--mute:#b3a6d4;--line:#3a2a6b;
--roxo:#a78bfa;--roxo2:#c4b5fd;--brilho:#8b5cf6;--ok:#6ee7b7;--ouro:#fcd34d;--alerta:#fbbf24;--ruim:#fca5a5}
@media (prefers-color-scheme:light){:root{--bg:#f6f2ff;--bg2:#ece4ff;--card:#fff;--card2:#f4efff;--fg:#24123f;
--mute:#6b5a8e;--line:#ddd0fa;--roxo:#7c3aed;--roxo2:#6d28d9;--brilho:#a78bfa;--ok:#047857;--ouro:#b45309;--alerta:#b45309;--ruim:#b91c1c}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 'Outfit',system-ui,sans-serif}
a{color:var(--roxo2)}
.wrap{max-width:1040px;margin:0 auto;padding:0 16px 48px}
.hero{position:relative;overflow:hidden;padding:36px 20px 28px;text-align:center;color:#fff;
background:radial-gradient(1200px 400px at 50% -10%,#7c3aed 0%,#4c1d95 45%,#1e0b3a 100%)}
.hero:before{content:"";position:absolute;inset:0;opacity:.55;background-image:
radial-gradient(2px 2px at 12% 30%,#fff,transparent),radial-gradient(1.5px 1.5px at 28% 70%,#e9d5ff,transparent),
radial-gradient(2px 2px at 46% 18%,#fff,transparent),radial-gradient(1.5px 1.5px at 63% 62%,#ddd6fe,transparent),
radial-gradient(2px 2px at 78% 26%,#fff,transparent),radial-gradient(1.5px 1.5px at 90% 74%,#e9d5ff,transparent),
radial-gradient(1px 1px at 8% 82%,#fff,transparent),radial-gradient(1px 1px at 54% 88%,#fff,transparent)}
.hero>*{position:relative}
.kr{font-family:'Noto Sans KR',sans-serif;letter-spacing:.3em;font-size:13px;opacity:.85}
.hero h1{font-size:clamp(30px,7vw,52px);line-height:1.05;margin:8px 0 6px;font-weight:800;letter-spacing:.02em}
.hero h1 span{background:linear-gradient(90deg,#f5d0fe,#c4b5fd,#a5b4fc);-webkit-background-clip:text;background-clip:text;color:transparent}
.hero .local{font-size:14px;opacity:.9}
.contagem{display:inline-flex;gap:10px;margin-top:18px;flex-wrap:wrap;justify-content:center}
.contagem div{background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.2);border-radius:14px;padding:8px 14px;min-width:74px}
.contagem b{display:block;font-size:26px;line-height:1.1}
.contagem small{font-size:11px;opacity:.8;text-transform:uppercase;letter-spacing:.08em}
.frase{margin-top:14px;font-size:14px;opacity:.9}
.atual{font-size:12px;opacity:.7;margin-top:8px}
.dedica{display:none;margin:12px auto 0;width:fit-content;padding:6px 16px;border-radius:99px;font-size:14px;font-weight:600;
background:linear-gradient(90deg,rgba(245,208,254,.25),rgba(165,180,252,.25));border:1px solid rgba(255,255,255,.35)}
h2{font-size:18px;margin:34px 0 12px;display:flex;align-items:center;gap:8px}
h2 small{font-weight:400;color:var(--mute);font-size:13px}
.datas{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:-22px;position:relative;z-index:2}
.data{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px;text-align:center;text-decoration:none;color:var(--fg);box-shadow:0 8px 24px rgba(20,0,60,.25)}
.data b{display:block;font-size:22px}
.data span{font-size:12px;color:var(--mute)}
.pill{display:inline-block;margin-top:6px;padding:2px 10px;border-radius:99px;font-size:12px;font-weight:600;background:var(--card2);color:var(--mute)}
.data.disponivel{border-color:var(--ok);box-shadow:0 0 0 2px var(--ok),0 0 28px var(--ok)} .data.disponivel .pill{background:var(--ok);color:#052e1c}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{background:var(--card);border:1px solid var(--line);border-radius:99px;padding:4px 10px;font-size:12px;color:var(--mute)}
.chip b{color:var(--fg);font-weight:600}
.grade{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px}
.box{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px 16px}
.box h3{margin:0 0 8px;font-size:15px;color:var(--roxo2)}
.linha{display:flex;justify-content:space-between;gap:8px;padding:5px 0;border-bottom:1px dashed var(--line);font-size:14px}
.linha:last-child{border-bottom:0}
.linha .v{font-weight:700;white-space:nowrap}
.ok{color:var(--ok)} .ouro{color:var(--ouro)} .ruim{color:var(--ruim)} .mute{color:var(--mute)}
.filtros{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}
.filtros button{font:inherit;font-size:13px;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:99px;padding:5px 12px;cursor:pointer}
.filtros button.on{background:var(--roxo);border-color:var(--roxo);color:#fff}
.anuncio{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--line);border-radius:14px;padding:12px 14px;display:flex;flex-direction:column;gap:6px}
.anuncio.bom{border-left-color:var(--ok)} .anuncio.golpe{border-left-color:var(--ruim);opacity:.8} .anuncio.semp{border-left-color:var(--alerta)}
.anuncio .topo{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.anuncio .preco{font-size:22px;font-weight:800}
.tags{display:flex;flex-wrap:wrap;gap:4px}
.tag{font-size:11px;padding:2px 8px;border-radius:99px;background:var(--card2);color:var(--mute)}
.selo{font-size:12px;font-weight:600}
.txt{font-size:13px;color:var(--mute);overflow-wrap:anywhere}
.btn{align-self:flex-start;font-size:13px;text-decoration:none;background:var(--roxo);color:#fff;border-radius:99px;padding:5px 14px}
.escondido{display:none}
table{border-collapse:collapse;width:100%;font-size:14px}
td,th{padding:7px 6px;border-bottom:1px solid var(--line);text-align:left} th{color:var(--mute);font-weight:600;font-size:12px}
.scroll{overflow-x:auto}
ul.limpa{list-style:none;padding:0;margin:0} ul.limpa li{padding:6px 0;border-bottom:1px solid var(--line)}
.guia ol{margin:0;padding-left:20px} .guia li{margin:6px 0}
#status{margin:12px auto 0;max-width:1040px;padding:0 16px;font-size:13px}
#status.vivo{text-align:center;color:var(--ok)}
#status.parado{display:block;background:#dc2626;color:#fff;border-radius:14px;padding:12px 16px;font-size:15px;text-align:center;box-shadow:0 0 24px rgba(220,38,38,.5)}
.ponto{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--ok);box-shadow:0 0 8px var(--ok);margin-right:4px;animation:pulsa 2s infinite}
@keyframes pulsa{50%{opacity:.3}}
footer{text-align:center;color:var(--mute);font-size:13px;margin-top:40px}
footer .chant{font-family:'Noto Sans KR',sans-serif;font-size:12px;letter-spacing:.04em;margin-top:6px;opacity:.8}
@media (max-width:560px){.datas{grid-template-columns:1fr 1fr 1fr;gap:6px}.data{padding:10px 4px}.data b{font-size:18px}}
"""

PAINEL_JS = """
(function(){
  // nome vem só do link (#Nome): não fica gravado na página pública nem chega ao servidor
  var nome=decodeURIComponent((location.hash||'').slice(1)).replace(/[^\\p{L} ]/gu,'').trim().slice(0,30);
  if(nome){
    document.title='Radar da '+nome+' 💜';
    document.getElementById('kr').textContent='보라해, '+nome;
    document.getElementById('frase').textContent='Radar da '+nome+': cada checagem é um passo mais perto de você no MorumBIS 💜';
    var d=document.getElementById('dedica');d.textContent='feito com 💜 especialmente pra '+nome;d.style.display='block';
  }
  var alvo=new Date('2026-10-28T20:00:00-03:00').getTime();
  function tick(){var d=Math.max(0,alvo-Date.now());var s=Math.floor(d/1000);
    var dias=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);
    var el=document.getElementById('cd');if(!el)return;
    el.innerHTML='<div><b>'+dias+'</b><small>dias</small></div><div><b>'+h+'</b><small>horas</small></div><div><b>'+m+'</b><small>min</small></div>';}
  tick();setInterval(tick,30000);
  // aviso de painel parado: ninguém atualizou (notebook desligado e/ou GitHub Actions parado)
  function frescor(){var g=new Date(document.body.getAttribute('data-gerado')).getTime();
    var min=Math.round((Date.now()-g)/60000);var st=document.getElementById('status');if(!st)return;
    if(min>35){var h=min>=120?Math.round(min/60)+' horas':min+' min';
      st.className='parado';st.innerHTML='⚠️ RADAR PARADO há '+h+'. Os preços e alertas abaixo podem estar velhos. <b>'+document.body.getAttribute('data-aviso')+'</b>';}
    else{st.className='vivo';st.innerHTML='<span class="ponto"></span> ao vivo · atualizado há '+Math.max(0,min)+' min';}}
  frescor();setInterval(frescor,60000);
  // painel da nuvem: status do Ticketmaster publicado pelo notebook
  var tmUrl=document.body.getAttribute('data-tm');
  function statusTm(){if(!tmUrl)return;fetch(tmUrl+'?t='+Date.now()).then(function(r){return r.json();}).then(function(j){
    var idade=(Date.now()-new Date(j.atualizado).getTime())/60000;
    var hora=new Date(j.atualizado).toLocaleString('pt-BR',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
    var nome={esgotado:'Esgotado',disponivel:'À VENDA!',erro:'sem resposta',desconhecido:'página mudou'};
    document.querySelectorAll('.data').forEach(function(c){var s=j.datas[c.getAttribute('data-d')];if(!s)return;
      c.querySelector('.pill').textContent=nome[s]||s;c.classList.toggle('disponivel',s==='disponivel');});
    var n=document.getElementById('nota-tm');if(!n)return;
    n.innerHTML=idade>35
      ?'<span style="display:inline-block;background:#dc2626;color:#fff;border-radius:12px;padding:6px 12px;font-weight:600">⚠️ Ticketmaster sem vigia desde '+hora+'. Abra o notebook pra voltar a checar.</span>'
      :'<span class="ok">● Ticketmaster checado pelo notebook às '+hora.split(' ').pop()+' · a cada 5 min</span>';
  }).catch(function(){});}
  statusTm();setInterval(statusTm,120000);
  var bts=document.querySelectorAll('.filtros button');
  bts.forEach(function(b){b.onclick=function(){bts.forEach(function(x){x.classList.remove('on')});b.classList.add('on');
    var f=b.getAttribute('data-f');document.querySelectorAll('.anuncio').forEach(function(a){
      a.classList.toggle('escondido',f!=='todos'&&!a.classList.contains(f));});};});
})();
"""


def gerar_painel(estado):
    tm = estado.get("ticketmaster", {})
    anuncios = sorted(estado.get("social", {}).get("anuncios", []), key=lambda a: a["criado"], reverse=True)
    noticias = sorted(estado.get("noticias", {}).get("itens", []), key=lambda n: n["data"], reverse=True)
    e = lambda s: html.escape(str(s or ""))
    teto = CONFIG["teto_preco"]
    dias_semana = {"28/10": "quarta", "30/10": "sexta", "31/10": "sábado"}

    rotulo = {"esgotado": "Esgotado", "disponivel": "À VENDA!", "erro": "sem resposta", "desconhecido": "página mudou"}
    tm_ativo = CONFIG.get("ticketmaster_ativo", True)
    datas = "".join(
        f'<a class="data {tm.get(d, {}).get("status", "") if tm_ativo else ""}" data-d="{d}" href="{e(u)}" target="_blank">'
        f'<b>{d}</b><span>{dias_semana.get(d, "")}</span><br>'
        f'<span class="pill">{rotulo.get(tm.get(d, {}).get("status"), "checando...") if tm_ativo else "Ticketmaster ↗"}</span></a>'
        for d, u in CONFIG["ticketmaster"]["datas"].items()
    )
    nota_tm = ("" if tm_ativo else '<div id="nota-tm" class="mute" style="text-align:center;font-size:13px;margin-top:8px">'
               "Ticketmaster é vigiado pelo notebook (bloqueia servidores), com alerta no celular via ntfy.</div>")
    status_tm_url = "" if tm_ativo else CONFIG.get("status_tm_url", "")

    # BuyTicket: um card por data com o menor preço de cada setor
    aceitas = CONFIG["buyticket"]["categorias_aceitas"]
    por_data = {}
    for chave, p in estado.get("buyticket", {}).get("precos", {}).items():
        data, setor, cat = chave.split("|")
        if cat in aceitas:
            atual = por_data.setdefault(data, {}).get(setor)
            if not atual or p["valor"] < atual[0]:
                por_data[data][setor] = (p["valor"], cat, p["url"])
    bt_cards = "".join(
        f'<div class="box"><h3>{d} · {dias_semana.get(d, "")}</h3>' + "".join(
            f'<div class="linha"><span>{e(setor)} <span class="mute">({e(cat)})</span></span>'
            f'<a class="v {"ouro" if v <= teto else ""}" href="{e(url)}" target="_blank">{brl(v)}</a></div>'
            for setor, (v, cat, url) in sorted(setores.items(), key=lambda kv: kv[1][0]))
        + "</div>"
        for d, setores in sorted(por_data.items())
    )

    def classe_anuncio(a):
        if "golpe" in a["selo"] or "evite" in a["selo"] or "desconfie" in a["selo"]:
            return "golpe"
        if a["selo"] in ("preço de face", "dentro do teto"):
            return "bom"
        return "semp" if a["selo"] == "sem preço" else "acima"
    cor_selo = {"bom": "ok", "golpe": "ruim", "semp": "ouro", "acima": "mute"}
    cards_anuncios = "".join(
        f'<div class="anuncio {classe_anuncio(a)}"><div class="topo">'
        f'<span class="preco">{brl(a["preco"]) if a["preco"] else "—"}</span>'
        f'<span class="mute" style="font-size:12px">{datetime.fromisoformat(a["criado"]):%d/%m %H:%M} · {e(a["fonte"])}</span></div>'
        f'<div class="tags">' + "".join(f'<span class="tag">{e(t)}</span>' for t in
                                        [a["setor"], a["categoria_txt"], ", ".join(a["datas"]), "@" + a["autor"]] if t and t != "@")
        + f'</div><span class="selo {cor_selo[classe_anuncio(a)]}">{e(a["selo"])}</span>'
        f'<span class="txt">{e(a["texto"][:180])}</span>'
        f'<a class="btn" href="{e(a["url"])}" target="_blank">Abrir anúncio</a></div>'
        for a in anuncios[:60]
    )

    stats = []
    for setor in CONFIG["precos_face"]:
        for cat in ("inteira", "meia"):
            vals = [a["preco"] for a in anuncios if a["setor"] == setor and a["categoria"] == cat
                    and a["preco"] and a["preco"] >= 150 and classe_anuncio(a) != "golpe"]
            if vals:
                stats.append(f"<tr><td>{setor.title()} {cat}</td><td class='ok'>{brl(preco_face(setor, cat))}</td>"
                             f"<td>{brl(min(vals))}</td><td>{brl(statistics.median(vals))}</td><td>{len(vals)}</td></tr>")

    buscas_x = [
        ("Vendo / repasso", '(vendo OR repasso OR repasse) ingresso bts -compro -procuro'),
        ("Preço de custo", 'bts ingresso ("preço de custo" OR "valor pago" OR "pelo valor")'),
        ("Por setor", 'vendo bts (arquibancada OR "cadeira superior" OR pista) -compro'),
        ("MorumBIS", 'bts morumbis ingresso vendo'),
    ]
    links_x = "".join(f'<a class="btn" href="https://x.com/search?q={quote(q)}&f=live" target="_blank">{e(n)}</a>'
                      for n, q in buscas_x)
    lista_noticias = "".join(
        f'<li><span class="mute" style="font-size:12px">{datetime.fromisoformat(n["data"]):%d/%m}</span> '
        f'<a href="{e(n["link"])}" target="_blank">{e(n["titulo"])}</a></li>'
        for n in noticias[:15]
    )

    execs = estado.get("ultimas_execucoes", {})
    quando = lambda k: f"{datetime.fromisoformat(execs[k]):%H:%M}" if k in execs else "—"
    fontes_chip = [("BuyTicket", "buyticket"), ("Bluesky", "social"), ("Reddit", "reddit"), ("Notícias", "noticias")]
    if tm_ativo:
        fontes_chip.insert(0, ("Ticketmaster", "ticketmaster"))
    chips = "".join(f'<span class="chip">{n} <b>{quando(k)}</b></span>' for n, k in fontes_chip)
    aviso_parado = ("Pode ser atraso do GitHub. Se passar de 1 hora, avise quem cuida do radar."
                    if os.environ.get("CI") else "Abra o notebook pra voltar a atualizar.")
    n_bons = sum(1 for a in anuncios if classe_anuncio(a) == "bom")

    PAINEL_FILE.write_text(f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="300"><title>Radar ARIRANG 💜</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;600;800&family=Noto+Sans+KR:wght@500&display=swap" rel="stylesheet">
<style>{PAINEL_CSS}</style></head><body data-gerado="{agora().isoformat()}" data-aviso="{e(aviso_parado)}" data-tm="{e(status_tm_url)}">
<div id="status"></div>
<header class="hero">
  <div class="kr" id="kr">보라해 · BORAHAE</div>
  <h1><span>ARIRANG</span><br>SÃO PAULO</h1>
  <div class="local">BTS WORLD TOUR · Estádio MorumBIS · 28 · 30 · 31 de outubro</div>
  <div class="contagem" id="cd"></div>
  <div class="frase" id="frase">Radar de ingressos: a cada checagem, um passo mais perto do MorumBIS 💜</div>
  <div class="dedica" id="dedica"></div>
  <div class="atual">atualizado {agora():%d/%m às %H:%M} · recarrega sozinho</div>
</header>
<main class="wrap">
  <div class="datas">{datas}</div>{nota_tm}

  <h2>🎟️ BuyTicket agora <small>menor preço · {", ".join(aceitas)} · dourado = até {brl(teto)}</small></h2>
  <div class="grade">{bt_cards or '<div class="box mute">sem dados ainda</div>'}</div>

  <h2>📣 Anúncios de fãs <small>Bluesky, Reddit e BuyTicket</small></h2>
  <div class="filtros">
    <button class="on" data-f="todos">Todos</button>
    <button data-f="bom">💜 Dentro do teto ({n_bons})</button>
    <button data-f="semp">Sem preço</button>
    <button data-f="golpe">🚩 Suspeitos</button>
  </div>
  <div class="grade">{cards_anuncios or '<div class="box mute">nenhum anúncio ainda</div>'}</div>

  <h2>⚖️ Preço justo x mercado <small>oficial já com taxa de 20%</small></h2>
  <div class="box scroll"><table><tr><th>Setor</th><th>Oficial</th><th>Menor anúncio</th><th>Mediana</th><th>Qtd</th></tr>
  {"".join(stats) or '<tr><td colspan="5" class="mute">sem dados ainda</td></tr>'}</table></div>

  <h2>🔎 Buscar no X <small>abre logado no seu X, aba "Mais recentes"</small></h2>
  <div class="chips">{links_x}</div>

  <h2>🛡️ Guia anti-golpe ARMY</h2>
  <div class="box guia"><ol>
    <li>Transferência só pelo app <b>Quentro</b>, e cada ingresso transfere <b>uma única vez</b>. Quem vende tem que ser quem comprou.</li>
    <li>Crie sua conta Quentro <b>antes</b>. Sem conta, a transferência fica "Pendente" e o vendedor pode cancelar.</li>
    <li>Prefira a <b>BuyTicket</b> (o dinheiro fica retido até o ingresso chegar) ou quem <b>transfere antes</b> de você pagar.</li>
    <li><b>Pix só pra chave pessoal no nome de quem está no pedido.</b> QR code de empresa ("eventos", "serviços", "maquininha") = golpe.</li>
    <li>Nunca passe o <b>código de login</b> que chega no seu e-mail.</li>
    <li>🚩 Frases de roteiro: "primeira vez vendendo", "te passo meus dados", "gastei no sound", "comprovo tudo", "dispenso golpista", "tags:".</li>
    <li>🚩 Conta antiga que ficou parada e voltou só pra vender pode ter sido invadida. Texto igual em várias contas = golpe.</li>
    <li>VIP/Soundcheck não transfere. Meia exige comprovante do mesmo tipo na entrada.</li>
  </ol></div>

  <h2>📰 Notícias</h2>
  <div class="box"><ul class="limpa">{lista_noticias or '<li class="mute">nenhuma recente</li>'}</ul></div>

  <h2>📡 Última checagem</h2>
  <div class="chips">{chips}</div>

  <footer>
    feito com 💜 por um ARMY · não é site oficial · Ticketmaster checado a cada poucos minutos
    <div class="chant">김남준! 김석진! 민윤기! 정호석! 박지민! 김태형! 전정국! BTS!</div>
  </footer>
</main>
<script>{PAINEL_JS}</script>
</body></html>""", encoding="utf-8")


def publicar_gist(arquivos):
    """Grava arquivos ({nome: texto}) no gist secreto da conta pessoal (token do gh, sem trocar a conta ativa)."""
    cfg = CONFIG["painel_online"]
    gh = shutil.which("gh") or r"C:\Program Files\GitHub CLI\gh.exe"
    token = subprocess.run([gh, "auth", "token", "--user", cfg["conta_github"]], capture_output=True,
                           text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
    if not token:
        log(f"gist: conta {cfg['conta_github']} não logada no gh")
        return
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    files = {nome: {"content": texto} for nome, texto in arquivos.items()}
    if cfg["gist_id"]:
        r = requests.patch(f"https://api.github.com/gists/{cfg['gist_id']}", json={"files": files},
                           headers=headers, timeout=TIMEOUT)
    else:
        r = requests.post("https://api.github.com/gists", headers=headers, timeout=TIMEOUT,
                          json={"files": files, "public": False, "description": "Monitor BTS Arirang"})
        r.raise_for_status()
        cfg["gist_id"] = r.json()["id"]
        (BASE / "config.json").write_text(json.dumps(CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"gist criado: {cfg['gist_id']}")
    r.raise_for_status()


def publicar_painel():
    publicar_gist({"painel.html": PAINEL_FILE.read_text(encoding="utf-8")})


def publicar_status_tm(estado):
    """Notebook -> gist: status das datas no Ticketmaster, lido pelo painel da nuvem (que é bloqueado lá)."""
    tm = estado.get("ticketmaster", {})
    datas = {d: tm.get(d, {}).get("status") for d in CONFIG["ticketmaster"]["datas"]}
    if datas == estado.get("status_tm_publicado") and not vencido(estado, "status_tm", 15):
        return
    publicar_gist({"ticketmaster.json": json.dumps({"atualizado": agora().isoformat(), "datas": datas})})
    estado["status_tm_publicado"] = datas
    marcar(estado, "status_tm")


# ---------------------------------------------------------------- main

def vencido(estado, chave, minutos):
    ultimo = estado.get("ultimas_execucoes", {}).get(chave)
    return not ultimo or agora() - datetime.fromisoformat(ultimo) >= timedelta(minutes=minutos)


def marcar(estado, chave):
    estado.setdefault("ultimas_execucoes", {})[chave] = agora().isoformat()


LOCK_FILE = BASE / "monitor.lock"


def uma_passada(forcar=False):
    if agora() > datetime.fromisoformat(CONFIG["fim_monitoramento"]):
        log("Monitoramento encerrado (passou do último show).")
        return
    # uma execução por vez (agendada e manual ao mesmo tempo sobrescreviam state.json)
    if LOCK_FILE.exists() and time.time() - LOCK_FILE.stat().st_mtime < 600:
        print("Outra execução em andamento, pulando.")
        return
    LOCK_FILE.write_text(str(os.getpid()))
    try:
        _passada(forcar)
    finally:
        LOCK_FILE.unlink(missing_ok=True)


def _passada(forcar):
    estado = carregar_estado()
    etapas = [
        ("ticketmaster", 0, checar_ticketmaster if CONFIG.get("ticketmaster_ativo", True) else None),
        ("social", CONFIG["intervalos_min"]["social"], checar_bluesky),
        ("buyticket", CONFIG["intervalos_min"]["buyticket"], checar_buyticket),
        ("x", CONFIG["intervalos_min"]["x"], checar_x),
        ("reddit", CONFIG["intervalos_min"]["reddit"], checar_reddit),
        ("noticias", CONFIG["intervalos_min"]["noticias"], checar_noticias),
    ]
    for chave, intervalo, func in etapas:
        if func and (forcar or vencido(estado, chave, intervalo)):
            try:
                func(estado)
                marcar(estado, chave)
            except Exception:
                log(f"erro em {chave}: {traceback.format_exc(limit=2)}")
            salvar_estado(estado)
    if CONFIG.get("status_tm_online") and CONFIG.get("ticketmaster_ativo", True):
        try:
            publicar_status_tm(estado)
            salvar_estado(estado)
        except Exception as e:
            log(f"status Ticketmaster online: {e}")
    resumo_diario(estado)
    salvar_estado(estado)
    gerar_painel(estado)
    if CONFIG["painel_online"]["ativo"] and (forcar or vencido(estado, "painel_online",
                                                               CONFIG["intervalos_min"]["painel_online"])):
        try:
            publicar_painel()
            marcar(estado, "painel_online")
            salvar_estado(estado)
        except Exception as e:
            log(f"painel online: {e}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--loop", action="store_true", help="rodar direto, uma passada a cada 5 min")
    ap.add_argument("--forcar", action="store_true", help="ignorar intervalos e checar tudo agora")
    ap.add_argument("--teste-notificacao", action="store_true")
    ap.add_argument("--silencioso", action="store_true", help="só registra no log, sem notificar")
    ap.add_argument("--login-x", action="store_true", help="abrir Edge pra logar no X (uma vez)")
    ap.add_argument("--avaliar", metavar="TEXTO", help="avaliar texto de anúncio colado (ex: post do X)")
    ap.add_argument("--novo-topico", action="store_true", help="gerar tópico ntfy próprio (instalação em outro PC)")
    args = ap.parse_args()

    if args.novo_topico:
        import secrets
        CONFIG["notificacao"]["ntfy_topico"] = "bts-arirang-" + secrets.token_hex(5)
        (BASE / "config.json").write_text(json.dumps(CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
        print(CONFIG["notificacao"]["ntfy_topico"])
        return

    if args.avaliar:
        estado = carregar_estado()
        a = analisar_texto(args.avaliar)
        if not a:
            print("Não parece anúncio de BTS no Brasil.")
            return
        _, selo = avaliar(a)
        selo = sinais_golpe(estado.get("social", {}), args.avaliar, "colado") or selo
        print(f"vendedor={a['vendedor']} preço={a['preco']} setor={a['setor']} categoria={a['categoria']} "
              f"datas={a['datas']} -> {selo}")
        return

    global SILENCIOSO
    SILENCIOSO = args.silencioso
    if os.environ.get("CI") and not STATE_FILE.exists():
        SILENCIOSO = True  # 1ª rodada na nuvem: só registra o que já existe, sem repetir alerta antigo
        log("Primeira rodada sem estado salvo: modo silencioso")

    if args.login_x:
        import fonte_x
        print("Janela do Edge aberta: faça login no X (até 10 min).")
        ok = fonte_x.login()
        print("Login OK, sessão salva em perfil_x/." if ok else "Login não concluído.")
        return

    if args.teste_notificacao:
        notificar("Teste monitor BTS", "Se chegou isso, notificação funciona.", PAINEL_FILE.as_uri())
        return
    if args.loop:
        while True:
            uma_passada(args.forcar)
            time.sleep(300)
    uma_passada(args.forcar)


if __name__ == "__main__":
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
