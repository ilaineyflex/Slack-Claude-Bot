import os
import re
import json
import tempfile
import threading
import requests
from groq import Groq
from docx import Document
from docx.shared import Pt, RGBColor, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

# ── Brand colors ──────────────────────────────────────────────────────────────
WINE       = RGBColor(0x7A, 0x00, 0x13)
GOLD       = RGBColor(0xD4, 0xA3, 0x47)
CHOCOLATE  = RGBColor(0x2B, 0x1B, 0x18)

# ── Config ────────────────────────────────────────────────────────────────────
GROQ_API_KEY     = os.environ.get("GROQ_API_KEY", "")
TAVILY_API_KEY   = os.environ.get("TAVILY_API_KEY", "")
SLACK_BOT_TOKEN  = os.environ.get("SLACK_BOT_TOKEN", "")

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

BRAND_DOCS_DIR = os.path.join(os.path.dirname(__file__), "brand_docs")

# Which docs to prioritize for each generation step (filenames only)
_GUIDE_VOICE_FILES = [
    "N2F CONTENT VOICE AND NAMING GUIDE_v1.md",
    "N2F Proprietary Coaching Mechanisms_v1.md",
    "Confident Yet Soft Brand Guide Mar 2026_v1.md",
    "markdown Premium Buyer OUTCOMES.md",
    "Confident Yet Soft PREMIUM Buyer MASTER SETUP PROMPT_v1.md",
]
_SELLING_FILES = [
    "N2F CONTENT VOICE AND NAMING GUIDE_v1.md",
    "High Ticket Buyer Content Guides_v1.md",
    "markdown Premium Buyer OUTCOMES.md",
    "ConfidentYetSoft_Premium_Buyers_PRISM_Content_Strategy_v1.md",
    "Leanne M Rich Buyer Guidance_v1.md",
]

# ── Brand doc loading ─────────────────────────────────────────────────────────
_BRAND_DOCS: dict[str, str] = {}

def _load_brand_docs():
    if not os.path.exists(BRAND_DOCS_DIR):
        print(f"brand_docs/ not found at {BRAND_DOCS_DIR}")
        return
    for fname in os.listdir(BRAND_DOCS_DIR):
        fpath = os.path.join(BRAND_DOCS_DIR, fname)
        if not os.path.isfile(fpath):
            continue
        try:
            if fname.endswith('.docx'):
                doc = Document(fpath)
                text = '\n'.join(p.text for p in doc.paragraphs if p.text.strip())
                _BRAND_DOCS[fname] = text
            elif fname.endswith(('.md', '.txt')):
                with open(fpath, 'r', encoding='utf-8', errors='ignore') as f:
                    _BRAND_DOCS[fname] = f.read()
        except Exception as e:
            print(f"Error loading brand doc {fname}: {e}")
    print(f"Loaded {len(_BRAND_DOCS)} brand docs")

_load_brand_docs()


def _get_brand_context(file_list, max_chars_each=5000):
    """Concatenate selected brand docs, each truncated to max_chars_each."""
    parts = []
    for fname in file_list:
        text = _BRAND_DOCS.get(fname, "")
        if text:
            parts.append(f"=== {fname} ===\n{text[:max_chars_each]}")
    return '\n\n'.join(parts)


# ── Groq helpers ──────────────────────────────────────────────────────────────
def _groq(prompt, temperature=0.4, max_tokens=4096):
    completion = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return completion.choices[0].message.content


def _parse_json(raw):
    """Strip markdown fences then parse JSON; fall back to first {...} block."""
    text = re.sub(r'^```(?:json)?\s*\n?', '', raw.strip(), flags=re.MULTILINE)
    text = re.sub(r'\n?```\s*$', '', text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r'\{[\s\S]*\}', text)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {}


# ── Step 1: Detect guide promise ──────────────────────────────────────────────
def detect_guide_promise(transcript, fathom_summary, client_name):
    """
    Returns {"promised": True, "topic": str, "client_context": str}
    or      {"promised": False, "topic": "", "client_context": ""}
    """
    combined = (fathom_summary or transcript or "")[:6000]
    prompt = f"""Analyze this call summary/transcript.

Did the COACH (not the client) explicitly commit to creating and sending a guide, plan, protocol, resource, or document to the client?

Look for phrases like: "I'll send you a guide", "I'll create a plan", "I'll put together", "I'll write up", "I'll prepare a", "I'll share a resource", "I'll make you a", "I'll email you a", "I'll build you a", "I'll send over a"

Client name: {client_name}

CALL CONTENT:
{combined}

Return ONLY valid JSON with no markdown:
{{"promised": true_or_false, "topic": "exact topic of the promised guide (empty string if false)", "client_context": "key details from the call relevant to the guide: symptoms, goals, specific concerns, current habits, measurements, medications — empty string if false"}}"""

    raw = _groq(prompt, temperature=0.2, max_tokens=512)
    result = _parse_json(raw)
    if not isinstance(result, dict):
        return {"promised": False, "topic": "", "client_context": ""}
    result.setdefault("promised", False)
    result.setdefault("topic", "")
    result.setdefault("client_context", "")
    return result


# ── Step 2: Market research ───────────────────────────────────────────────────
def run_guide_market_research(topic):
    """3 Tavily searches. Returns list of {"query": str, "findings": str}."""
    if not TAVILY_API_KEY:
        print("TAVILY_API_KEY not set — skipping market research")
        return []
    queries = [
        f"PCOS {topic} questions women asking reddit quora 2025 2026",
        f"{topic} women hormonal health digital guide ebook best selling 2025 2026",
        f"PCOS {topic} lead magnet freebie trending instagram tiktok wellness 2026",
    ]
    results = []
    for q in queries:
        try:
            resp = requests.post(
                "https://api.tavily.com/search",
                json={"api_key": TAVILY_API_KEY, "query": q, "search_depth": "basic", "max_results": 4, "include_answer": True},
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                answer = data.get("answer", "")
                items = data.get("results", [])
                snippet = answer + "\n" + "\n".join(
                    f"- {r.get('title', '')}: {r.get('content', '')[:200]}" for r in items[:3]
                )
                results.append({"query": q, "findings": snippet[:800]})
            else:
                print(f"Tavily {resp.status_code} for: {q}")
        except Exception as e:
            print(f"Tavily error '{q}': {e}")
    return results


# ── Step 3: Generate guide content ────────────────────────────────────────────
def generate_client_guide(topic, client_context, client_name):
    """
    Returns {
        "guide_name": str,
        "intro": str,
        "sections": [{"heading", "content", "review_note"}],
        "note_to_send_to_client": str,
        "keyword": str
    }
    """
    brand_ctx = _get_brand_context(_GUIDE_VOICE_FILES, max_chars_each=5000)
    prompt = f"""You are creating a client-facing educational guide for New To Fitness, Elaine Acheampong's premium PCOS and hormone health coaching brand.

BRAND GUIDANCE — follow every naming, voice, and tone rule exactly:
{brand_ctx}

TASK: Create a comprehensive, evergreen guide on: "{topic}"

CLIENT CONTEXT (use to personalize the intro and note to client only — guide content itself must be universal and reusable for any client with this need):
Client: {client_name}
Call notes: {client_context}

REQUIREMENTS:
1. ONLY scientifically proven information — cite mechanisms ("cortisol disrupts insulin signalling"), not vague claims
2. Reference N2F proprietary mechanisms where genuinely relevant (Hormone-First Approach, HormoneSync Movement, 10-Hour Hormone Optimization Window, Hormonal Driver Assessment)
3. Written for an ambitious woman with PCOS: intelligent, emotionally aware, overwhelmed by conflicting advice
4. Tone: Confident Yet Soft — authoritative but warm, never clinical or preachy, outcome-first language
5. Evergreen — reusable for any future client with this need, not just {client_name}
6. 4-6 sections, each containing actionable content the client can implement immediately
7. "review_note" field: flag any section where scientific accuracy needs Elaine's verification or brand voice needs refinement — leave empty string if confident

Return ONLY valid JSON with no markdown fences:
{{"guide_name": "Premium-buyer-aligned name following N2F naming conventions", "intro": "1-2 paragraph outcome-first introduction in brand voice", "sections": [{{"heading": "Section heading", "content": "Full section content 3-5 paragraphs actionable and scientifically grounded", "review_note": "What to verify or update — empty string if confident"}}], "note_to_send_to_client": "Warm 3-4 sentence message from Elaine to {client_name} introducing the guide referencing something specific from their call Confident Yet Soft tone", "keyword": "Specific trigger keyword for this guide type — must NOT be a word clients say in general conversation (avoid: belly weight period bloat stress food energy) — use a specific phrase unique to this guide such as cortisol protocol or eating window timing"}}"""

    raw = _groq(prompt, temperature=0.4, max_tokens=4000)
    result = _parse_json(raw)
    if not result or not result.get("sections"):
        result = {
            "guide_name": f"N2F {topic.title()} Guide",
            "intro": f"This guide covers {topic} for women managing PCOS.",
            "sections": [{"heading": "Overview", "content": raw[:2000], "review_note": "Full review needed — AI output not parsed as JSON"}],
            "note_to_send_to_client": f"Hi {client_name}, here is the guide we discussed on our call.",
            "keyword": topic.split()[0].lower() if topic else "guide",
        }
    return result


# ── Step 4: Selling opportunities ─────────────────────────────────────────────
def generate_selling_opportunities_content(guide_data, topic, search_results):
    """
    Returns {
        "upsell_products": [...],
        "lead_magnets": [...],
        "carousel_scripts": [...],
        "automation_keyword": str
    }
    """
    brand_ctx = _get_brand_context(_SELLING_FILES, max_chars_each=4000)
    guide_name = guide_data.get("guide_name", topic)
    search_text = "\n".join(f"[Search: {r['query']}]\n{r['findings']}" for r in search_results) if search_results else "No live search data."

    prompt = f"""You are a digital product strategist for New To Fitness, Elaine Acheampong's premium PCOS hormone health coaching brand.

BRAND GUIDANCE:
{brand_ctx}

CLIENT GUIDE:
Name: {guide_name}
Topic: {topic}

LIVE MARKET RESEARCH:
{search_text}

TASK: Create a selling opportunities analysis.

RULES:
- All product and freebie names follow N2F naming conventions: outcome-first, premium buyer aligned, no clinical jargon
- Rank upsell products by revenue potential — justify each ranking using the market research above plus proven digital product conversion principles
- A lead magnet name that appears in the top 5 upsell list MUST NOT appear in the lead magnets list (no free version of something you charge for)
- 5 carousel scripts total: mix of the #1 digital product and #1 lead magnet
- automation_keyword must be specific enough to not trigger when clients use the word casually

Return ONLY valid JSON with no markdown fences:
{{"upsell_products": [{{"rank": 1, "name": "Name", "subheader": "Subheader", "price_point": "$X", "score": 95, "reasoning": "Why this will sell based on market research and buyer psychology"}}], "lead_magnets": [{{"rank": 1, "name": "Name", "subheader": "Subheader", "platform": "Instagram/TikTok/Both", "engagement_prediction": "Why this will perform well"}}], "carousel_scripts": [{{"promotes": "product or lead magnet name", "caption": "Full Instagram caption with hook body and CTA", "slides": ["Slide 1 copy", "Slide 2 copy", "Slide 3 copy", "Slide 4 copy", "Slide 5 copy", "Slide 6 copy"]}}], "automation_keyword": "specific keyword phrase"}}

Include 3-5 upsell products, 3-5 lead magnets, 5 carousel scripts."""

    raw = _groq(prompt, temperature=0.5, max_tokens=5000)
    result = _parse_json(raw)
    if not isinstance(result, dict):
        result = {"upsell_products": [], "lead_magnets": [], "carousel_scripts": [], "automation_keyword": ""}
    return result


# ── Step 5: Digital product content ──────────────────────────────────────────
def generate_digital_product_content(guide_data, selling_data):
    """Full digital product version of the #1 upsell."""
    brand_ctx = _get_brand_context(_GUIDE_VOICE_FILES, max_chars_each=3500)
    upsells = selling_data.get("upsell_products", [])
    top = upsells[0] if upsells else {}
    product_name = top.get("name", guide_data.get("guide_name", ""))
    subheader = top.get("subheader", "")
    topic = guide_data.get("guide_name", "")
    guide_summary = "\n".join(
        f"- {s.get('heading')}: {s.get('content', '')[:200]}..."
        for s in guide_data.get("sections", [])
    )

    prompt = f"""You are creating a PREMIUM DIGITAL PRODUCT for New To Fitness by Elaine Acheampong.

BRAND GUIDANCE:
{brand_ctx}

PRODUCT:
Name: {product_name}
Subheader: {subheader}
Topic: {topic}

EXISTING CLIENT GUIDE STRUCTURE (expand and deepen significantly):
{guide_summary}

This digital product must be SIGNIFICANTLY MORE DETAILED than a free guide:
- 6-8 sections minimum
- Include worksheets or self-assessment exercises where appropriate
- Include a 7-day or 4-week implementation plan
- Deeper on mechanisms, science, and individualization
- Premium buyer quality: thorough, specific, outcome-driven
- Evergreen: works for any N2F client with this need

Return ONLY valid JSON with no markdown fences:
{{"product_name": "{product_name}", "subheader": "{subheader}", "intro": "2-3 paragraph premium welcome introduction", "sections": [{{"heading": "Heading", "content": "Full content 5-8 paragraphs", "has_exercise": false, "exercise": "", "review_note": "What to verify — empty if confident"}}], "implementation_plan": {{"title": "Plan title", "days_or_weeks": [{{"label": "Day 1", "focus": "Focus area", "actions": ["action 1", "action 2"]}}]}}}}"""

    raw = _groq(prompt, temperature=0.4, max_tokens=6000)
    result = _parse_json(raw)
    if not isinstance(result, dict) or not result.get("sections"):
        result = {
            "product_name": product_name, "subheader": subheader,
            "intro": f"Welcome to {product_name}.",
            "sections": [{"heading": "Content", "content": raw[:3000], "has_exercise": False, "exercise": "", "review_note": "Full review needed — parse error"}],
            "implementation_plan": {"title": "Your Plan", "days_or_weeks": []}
        }
    return result


# ── Step 6: Lead magnet content ───────────────────────────────────────────────
def generate_lead_magnet_content(guide_data, selling_data):
    """Freebie version of the #1 lead magnet."""
    brand_ctx = _get_brand_context(_GUIDE_VOICE_FILES, max_chars_each=3000)
    lead_magnets = selling_data.get("lead_magnets", [])
    top = lead_magnets[0] if lead_magnets else {}
    freebie_name = top.get("name", f"{guide_data.get('guide_name', 'Guide')} — Free Guide")
    subheader = top.get("subheader", "")
    topic = guide_data.get("guide_name", "")

    prompt = f"""You are creating a FREE LEAD MAGNET for New To Fitness by Elaine Acheampong.

BRAND GUIDANCE:
{brand_ctx}

LEAD MAGNET:
Name: {freebie_name}
Subheader: {subheader}
Topic: {topic}

RULES FOR A LEAD MAGNET:
- Genuinely valuable and immediately useful — builds trust and demonstrates expertise
- Deliberately INCOMPLETE — creates desire for the full digital product without feeling like a tease
- 3-4 sections maximum; quick-win focused; something implementable today
- Ends with a clear invitation: join the N2F private WhatsApp community OR upgrade to the full guide
- Premium buyer aesthetic but lighter format (beautiful checklist or mini-guide, not a course)

Return ONLY valid JSON with no markdown fences:
{{"freebie_name": "{freebie_name}", "subheader": "{subheader}", "intro": "1 paragraph hook — what this delivers and why it matters right now", "sections": [{{"heading": "Section heading", "content": "Valuable but incomplete content that builds desire for more", "review_note": "What to verify — empty if confident"}}], "cta_section": {{"heading": "What comes next", "community_invite": "Text inviting them to join the N2F private WhatsApp community", "upgrade_invite": "Text teasing the full digital product and what they get by upgrading"}}}}"""

    raw = _groq(prompt, temperature=0.4, max_tokens=3500)
    result = _parse_json(raw)
    if not isinstance(result, dict) or not result.get("sections"):
        result = {
            "freebie_name": freebie_name, "subheader": subheader,
            "intro": f"This free guide introduces {topic}.",
            "sections": [{"heading": "Content", "content": raw[:2000], "review_note": "Full review needed — parse error"}],
            "cta_section": {"heading": "Next Steps", "community_invite": "Join our community.", "upgrade_invite": "Upgrade for the full guide."}
        }
    return result


# ── Word doc helpers ───────────────────────────────────────────────────────────
def _set_shading(para, fill_hex):
    pPr = para._p.get_or_add_pPr()
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'), 'clear')
    shd.set(qn('w:color'), 'auto')
    shd.set(qn('w:fill'), fill_hex.lstrip('#'))
    pPr.append(shd)


def _gold_rule(doc):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(2)
    pPr = p._p.get_or_add_pPr()
    pBdr = OxmlElement('w:pBdr')
    bottom = OxmlElement('w:bottom')
    bottom.set(qn('w:val'), 'single')
    bottom.set(qn('w:sz'), '6')
    bottom.set(qn('w:space'), '1')
    bottom.set(qn('w:color'), 'D4A347')
    pBdr.append(bottom)
    pPr.append(pBdr)


def _review_callout(doc, review_note):
    if not review_note:
        return
    p = doc.add_paragraph()
    _set_shading(p, 'FFF3CD')
    run = p.add_run(f"⚠ REVIEW FOR ELAINE: {review_note}")
    run.font.italic = True
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor(0x7A, 0x00, 0x13)
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after = Pt(8)


def _claude_prompt_callout(doc):
    doc.add_paragraph()
    p = doc.add_paragraph()
    _set_shading(p, 'EDF4FB')
    run = p.add_run(
        "\U0001f4cb TO UPDATE BRAND VOICE: Copy any section into Claude with this prompt: "
        "\"Rewrite the following for N2F Confident Yet Soft premium buyer voice. "
        "Client is an ambitious woman with PCOS who wants outcomes, not clinical explanations. "
        "Use outcome-first language, remove jargon, warm but authoritative tone. "
        "Reference N2F proprietary mechanisms where relevant.\""
    )
    run.font.italic = True
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor(0x1E, 0x1A, 0x1A)
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after = Pt(4)


def _setup_doc(doc):
    style = doc.styles['Normal']
    style.font.name = 'Calibri'
    style.font.size = Pt(11)
    style.font.color.rgb = CHOCOLATE
    for section in doc.sections:
        section.top_margin = Inches(1)
        section.bottom_margin = Inches(1)
        section.left_margin = Inches(1.2)
        section.right_margin = Inches(1.2)


def _header_block(doc, title, subtitle=None):
    brand = doc.add_paragraph()
    r = brand.add_run("NEW TO FITNESS")
    r.font.size = Pt(9); r.font.bold = True; r.font.color.rgb = GOLD
    brand.paragraph_format.space_before = Pt(0)
    brand.paragraph_format.space_after = Pt(4)

    title_p = doc.add_paragraph()
    r = title_p.add_run(title)
    r.font.size = Pt(22); r.font.bold = True; r.font.color.rgb = WINE
    title_p.paragraph_format.space_after = Pt(4)

    if subtitle:
        sub = doc.add_paragraph()
        r = sub.add_run(subtitle)
        r.font.size = Pt(12); r.font.italic = True; r.font.color.rgb = CHOCOLATE
        sub.paragraph_format.space_after = Pt(8)

    _gold_rule(doc)
    doc.add_paragraph()


def _section(doc, heading, content, review_note=""):
    h = doc.add_paragraph()
    r = h.add_run(heading)
    r.font.size = Pt(13); r.font.bold = True; r.font.color.rgb = WINE
    h.paragraph_format.space_before = Pt(14)
    h.paragraph_format.space_after = Pt(4)

    for line in content.split('\n'):
        line = line.strip()
        if not line:
            continue
        p = doc.add_paragraph()
        p.add_run(line).font.size = Pt(11)
        p.paragraph_format.space_after = Pt(6)

    _review_callout(doc, review_note)


def _footer(doc, text):
    p = doc.add_paragraph()
    r = p.add_run(text)
    r.font.size = Pt(8); r.font.color.rgb = GOLD; r.font.italic = True
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER


def _tmppath(prefix):
    fd, path = tempfile.mkstemp(suffix='.docx', prefix=prefix)
    os.close(fd)
    return path


# ── Word doc builders ──────────────────────────────────────────────────────────
def build_client_guide_docx(guide_data, client_name):
    doc = Document()
    _setup_doc(doc)
    _header_block(doc, guide_data.get("guide_name", "Your Guide"),
                  f"Prepared for you by Elaine Acheampong | New To Fitness")

    for line in guide_data.get("intro", "").split('\n'):
        if line.strip():
            p = doc.add_paragraph(line.strip())
            p.paragraph_format.space_after = Pt(6)
    doc.add_paragraph()

    for s in guide_data.get("sections", []):
        _section(doc, s.get("heading", ""), s.get("content", ""), s.get("review_note", ""))

    _claude_prompt_callout(doc)
    doc.add_paragraph()
    _footer(doc, "New To Fitness | @ilaineyflex | Educational purposes only — not medical advice.")

    path = _tmppath('guide_')
    doc.save(path)
    return path


def build_selling_opportunities_docx(selling_data, guide_name):
    doc = Document()
    _setup_doc(doc)
    _header_block(doc, f"{guide_name} — Selling Opportunities", "Digital product strategy assessment")

    # Upsell products
    h = doc.add_paragraph()
    r = h.add_run("DIGITAL PRODUCT UPSELL OPPORTUNITIES")
    r.font.bold = True; r.font.size = Pt(14); r.font.color.rgb = WINE
    _gold_rule(doc)

    for prod in selling_data.get("upsell_products", []):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(12)
        r = p.add_run(f"#{prod.get('rank')} — {prod.get('name', '')}")
        r.font.bold = True; r.font.size = Pt(12); r.font.color.rgb = WINE

        sub = doc.add_paragraph()
        sub.add_run(prod.get("subheader", "")).font.italic = True

        meta = doc.add_paragraph()
        meta.add_run(f"Price point: {prod.get('price_point', '')}  |  Score: {prod.get('score', '')}").font.size = Pt(10)

        reason = doc.add_paragraph(prod.get("reasoning", ""))
        reason.paragraph_format.space_after = Pt(8)

    doc.add_paragraph()

    # Lead magnets
    h2 = doc.add_paragraph()
    r2 = h2.add_run("LEAD MAGNETS & FREEBIES")
    r2.font.bold = True; r2.font.size = Pt(14); r2.font.color.rgb = WINE
    _gold_rule(doc)

    for lm in selling_data.get("lead_magnets", []):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(12)
        r = p.add_run(f"#{lm.get('rank')} — {lm.get('name', '')}")
        r.font.bold = True; r.font.size = Pt(12); r.font.color.rgb = CHOCOLATE

        sub = doc.add_paragraph()
        sub.add_run(lm.get("subheader", "")).font.italic = True

        meta = doc.add_paragraph()
        meta.add_run(f"Platform: {lm.get('platform', '')}").font.size = Pt(10)

        pred = doc.add_paragraph(lm.get("engagement_prediction", ""))
        pred.paragraph_format.space_after = Pt(8)

    doc.add_paragraph()

    # Carousel scripts
    h3 = doc.add_paragraph()
    r3 = h3.add_run("CAROUSEL SCRIPTS")
    r3.font.bold = True; r3.font.size = Pt(14); r3.font.color.rgb = WINE
    _gold_rule(doc)

    for i, script in enumerate(selling_data.get("carousel_scripts", []), 1):
        cs = doc.add_paragraph()
        cs.paragraph_format.space_before = Pt(14)
        cs.add_run(f"CAROUSEL {i} — Promoting: {script.get('promotes', '')}").font.bold = True

        cap_label = doc.add_paragraph()
        r = cap_label.add_run("CAPTION:")
        r.font.bold = True; r.font.size = Pt(10); r.font.color.rgb = GOLD

        cap = doc.add_paragraph(script.get("caption", ""))
        cap.paragraph_format.space_after = Pt(6)

        slides_label = doc.add_paragraph()
        r = slides_label.add_run("SLIDE COPY:")
        r.font.bold = True; r.font.size = Pt(10); r.font.color.rgb = GOLD

        for j, slide in enumerate(script.get("slides", []), 1):
            sp = doc.add_paragraph()
            sp.add_run(f"Slide {j}: ").font.bold = True
            sp.runs[0].font.size = Pt(10)
            sp.add_run(slide).font.size = Pt(10)
            sp.paragraph_format.space_after = Pt(3)

    # Automation keyword
    doc.add_paragraph()
    kw = doc.add_paragraph()
    r = kw.add_run(f"AUTOMATION KEYWORD: {selling_data.get('automation_keyword', '')}")
    r.font.bold = True; r.font.color.rgb = WINE

    path = _tmppath('selling_')
    doc.save(path)
    return path


def build_digital_product_docx(product_data):
    doc = Document()
    _setup_doc(doc)
    _header_block(doc, product_data.get("product_name", "Digital Product"),
                  product_data.get("subheader", ""))

    for line in product_data.get("intro", "").split('\n'):
        if line.strip():
            p = doc.add_paragraph(line.strip())
            p.paragraph_format.space_after = Pt(8)
    doc.add_paragraph()

    for s in product_data.get("sections", []):
        _section(doc, s.get("heading", ""), s.get("content", ""), s.get("review_note", ""))
        if s.get("has_exercise") and s.get("exercise"):
            ex = doc.add_paragraph()
            _set_shading(ex, 'F0F8E8')
            r = ex.add_run(f"EXERCISE / WORKSHEET:\n{s['exercise']}")
            r.font.size = Pt(10)
            r.font.color.rgb = RGBColor(0x1A, 0x4A, 0x1A)
            ex.paragraph_format.space_after = Pt(10)

    plan = product_data.get("implementation_plan", {})
    if plan and plan.get("days_or_weeks"):
        doc.add_paragraph()
        ph = doc.add_paragraph()
        r = ph.add_run(plan.get("title", "Your Implementation Plan"))
        r.font.size = Pt(14); r.font.bold = True; r.font.color.rgb = WINE
        _gold_rule(doc)

        for day in plan.get("days_or_weeks", []):
            dp = doc.add_paragraph()
            dp.paragraph_format.space_before = Pt(10)
            r = dp.add_run(day.get("label", ""))
            r.font.bold = True; r.font.color.rgb = CHOCOLATE

            fp = doc.add_paragraph()
            fp.add_run(f"Focus: {day.get('focus', '')}").font.italic = True

            for action in day.get("actions", []):
                ap = doc.add_paragraph(f"• {action}")
                ap.paragraph_format.left_indent = Inches(0.3)

    _claude_prompt_callout(doc)
    doc.add_paragraph()
    _footer(doc, "New To Fitness | @ilaineyflex | Educational purposes only — not medical advice.")

    path = _tmppath('digital_product_')
    doc.save(path)
    return path


def build_lead_magnet_docx(freebie_data):
    doc = Document()
    _setup_doc(doc)
    _header_block(doc, freebie_data.get("freebie_name", "Free Guide"),
                  freebie_data.get("subheader", ""))

    intro = freebie_data.get("intro", "")
    if intro:
        p = doc.add_paragraph(intro)
        p.paragraph_format.space_after = Pt(10)

    for s in freebie_data.get("sections", []):
        _section(doc, s.get("heading", ""), s.get("content", ""), s.get("review_note", ""))

    cta = freebie_data.get("cta_section", {})
    if cta:
        doc.add_paragraph()
        ch = doc.add_paragraph()
        r = ch.add_run(cta.get("heading", "What comes next"))
        r.font.size = Pt(13); r.font.bold = True; r.font.color.rgb = WINE
        _gold_rule(doc)

        comm = doc.add_paragraph()
        _set_shading(comm, 'FFF8F0')
        comm.add_run(cta.get("community_invite", "")).font.size = Pt(11)
        comm.paragraph_format.space_after = Pt(8)

        upg = doc.add_paragraph()
        _set_shading(upg, 'F8F0FF')
        upg.add_run(cta.get("upgrade_invite", "")).font.size = Pt(11)
        upg.paragraph_format.space_after = Pt(8)

    _claude_prompt_callout(doc)
    doc.add_paragraph()
    _footer(doc, "New To Fitness | @ilaineyflex | Free guide — share with someone who needs this.")

    path = _tmppath('lead_magnet_')
    doc.save(path)
    return path


# ── Slack file upload ──────────────────────────────────────────────────────────
def upload_file_to_slack(channel_id, thread_ts, file_path, filename, comment=""):
    """Upload using the v2 Slack API (getUploadURLExternal + completeUploadExternal)."""
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    file_size = os.path.getsize(file_path)

    resp = requests.get(
        "https://slack.com/api/files.getUploadURLExternal",
        headers=headers,
        params={"filename": filename, "length": file_size},
    )
    data = resp.json()
    if not data.get("ok"):
        print(f"Slack getUploadURL failed: {data.get('error')}")
        return False

    upload_url = data["upload_url"]
    file_id = data["file_id"]

    with open(file_path, 'rb') as f:
        up = requests.post(upload_url, data=f)
    if up.status_code not in (200, 204):
        print(f"Slack file content upload failed: {up.status_code}")
        return False

    payload = {"files": [{"id": file_id, "title": filename}], "channel_id": channel_id}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    if comment:
        payload["initial_comment"] = comment

    complete = requests.post(
        "https://slack.com/api/files.completeUploadExternal",
        headers={**headers, "Content-Type": "application/json"},
        json=payload,
    )
    result = complete.json()
    print(f"Slack upload: ok={result.get('ok')} {result.get('error', '')}")
    return result.get("ok", False)


# ── Full guide pipeline ────────────────────────────────────────────────────────
def run_guide_pipeline(client_name, topic, client_context, transcript, fathom_summary,
                       channel_id, thread_ts, projects_channel_id):
    """Orchestrates all 6 generation steps. Designed to run in a background thread."""
    print(f"\n=== Guide pipeline: {client_name} | {topic} ===")
    guide_path = selling_path = digital_path = lm_path = None

    try:
        # 1. Client guide
        print("Step 1: generating client guide...")
        guide_data = generate_client_guide(topic, client_context, client_name)
        guide_name = guide_data.get("guide_name", topic)
        note = guide_data.get("note_to_send_to_client", "")

        guide_path = build_client_guide_docx(guide_data, client_name)
        safe = re.sub(r'[^\w\s-]', '', guide_name).strip().replace(' ', '_')
        upload_file_to_slack(
            channel_id, thread_ts, guide_path, f"{safe}.docx",
            f"*Note to send to client:*\n{note}"
        )
        print(f"Client guide posted for {client_name}")

        # 2. Market research
        print("Step 2: market research...")
        search_results = run_guide_market_research(topic)

        # 3. Selling opportunities
        print("Step 3: selling opportunities...")
        selling_data = generate_selling_opportunities_content(guide_data, topic, search_results)
        selling_path = build_selling_opportunities_docx(selling_data, guide_name)

        # 4. Digital product
        print("Step 4: digital product...")
        digital_data = generate_digital_product_content(guide_data, selling_data)
        digital_path = build_digital_product_docx(digital_data)
        digital_name = re.sub(r'[^\w\s-]', '', digital_data.get("product_name", guide_name)).strip().replace(' ', '_')

        # 5. Lead magnet
        print("Step 5: lead magnet...")
        lm_data = generate_lead_magnet_content(guide_data, selling_data)
        lm_path = build_lead_magnet_docx(lm_data)
        lm_name = re.sub(r'[^\w\s-]', '', lm_data.get("freebie_name", guide_name)).strip().replace(' ', '_')

        # 6. Post 3 docs to #projects-launches-summaries
        if projects_channel_id:
            keyword = selling_data.get("automation_keyword", guide_data.get("keyword", ""))
            intro_text = (
                f"*Guide Packet: {guide_name}*\n"
                f"Generated from {client_name}'s call | Topic: {topic}\n"
                f"Automation keyword: `{keyword}`\n"
                f"3 documents ready for review:"
            )
            headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
            intro_resp = requests.post(
                "https://slack.com/api/chat.postMessage",
                headers=headers,
                json={"channel": projects_channel_id, "text": intro_text},
            )
            intro_ts = intro_resp.json().get("ts") if intro_resp.json().get("ok") else None

            upload_file_to_slack(projects_channel_id, intro_ts, selling_path,
                                 f"{safe}_Selling_Opportunities.docx", "Selling Opportunities Assessment")
            upload_file_to_slack(projects_channel_id, intro_ts, digital_path,
                                 f"{digital_name}_Digital_Product.docx", "Digital Product (Full Version)")
            upload_file_to_slack(projects_channel_id, intro_ts, lm_path,
                                 f"{lm_name}_Lead_Magnet.docx", "Lead Magnet / Freebie Version")

            print(f"All 3 docs posted to #projects-launches-summaries")

        print(f"=== Guide pipeline complete: {client_name} | {guide_name} ===")

    except Exception as e:
        print(f"Guide pipeline error for {client_name}: {e}")
        import traceback
        traceback.print_exc()
    finally:
        for p in [guide_path, selling_path, digital_path, lm_path]:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass
