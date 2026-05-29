import os
import re
import json
import tempfile
import requests
from groq import Groq
from docx import Document
from docx.shared import Pt, RGBColor, Inches, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

# ── Brand colors ──────────────────────────────────────────────────────────────
WINE       = RGBColor(0x7A, 0x00, 0x13)
GOLD       = RGBColor(0xD4, 0xA3, 0x47)
CHOCOLATE  = RGBColor(0x2B, 0x1B, 0x18)
CREAM      = RGBColor(0xFA, 0xF5, 0xEF)
WHITE      = RGBColor(0xFF, 0xFF, 0xFF)

# ── Config ────────────────────────────────────────────────────────────────────
GROQ_API_KEY    = os.environ.get("GROQ_API_KEY", "")
TAVILY_API_KEY  = os.environ.get("TAVILY_API_KEY", "")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

BRAND_DOCS_DIR = os.path.join(os.path.dirname(__file__), "brand_docs")

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
_BRAND_DOCS = {}  # type: dict

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


def _get_brand_context(file_list, max_chars_each=4000):
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


# ── Task extraction ───────────────────────────────────────────────────────────
_GUIDE_NOUNS  = {'guide', 'plan', 'protocol', 'resource', 'worksheet', 'checklist',
                 'document', 'program', 'template', 'assessment', 'framework', 'roadmap'}
_CREATE_VERBS = {'create', 'send', 'prepare', 'write', 'build', 'make', 'develop',
                 'share', 'email', 'put together'}

def extract_guide_from_tasks(coach_tasks):
    for task in (coach_tasks or []):
        task_lower = task.lower()
        if not (any(v in task_lower for v in _CREATE_VERBS) and
                any(n in task_lower for n in _GUIDE_NOUNS)):
            continue
        m = re.search(
            r'(?:the|a|an)\s+([\w\s\-\'&,]+?'
            r'(?:Guide|Plan|Protocol|Resource|Worksheet|Checklist|Document|'
            r'Program|Template|Assessment|Framework|Roadmap))',
            task, re.IGNORECASE
        )
        guide_name = m.group(1).strip() if m else task.strip()
        return {"found": True, "guide_name": guide_name, "full_task": task}
    return {"found": False, "guide_name": "", "full_task": ""}


def detect_guide_promise(transcript, fathom_summary, client_name):
    combined = (fathom_summary or transcript or "")[:6000]
    prompt = f"""Analyze this call. Did the coach explicitly commit to creating and sending a guide, plan, or document to {client_name}?

CALL:
{combined}

Return ONLY valid JSON:
{{"promised": true_or_false, "topic": "exact topic (empty if false)", "client_context": "key call details (empty if false)"}}"""
    result = _parse_json(_groq(prompt, temperature=0.2, max_tokens=400))
    if not isinstance(result, dict):
        return {"promised": False, "topic": "", "client_context": ""}
    result.setdefault("promised", False)
    result.setdefault("topic", "")
    result.setdefault("client_context", "")
    return result


# ── Market research ───────────────────────────────────────────────────────────
def run_guide_market_research(topic):
    if not TAVILY_API_KEY:
        print("TAVILY_API_KEY not set — skipping market research")
        return []
    queries = [
        f"PCOS {topic} questions women asking reddit quora 2025 2026",
        f"{topic} women hormonal health digital guide best selling 2025 2026",
        f"PCOS {topic} lead magnet trending instagram tiktok 2026",
    ]
    results = []
    for q in queries:
        try:
            resp = requests.post(
                "https://api.tavily.com/search",
                json={"api_key": TAVILY_API_KEY, "query": q, "search_depth": "basic",
                      "max_results": 4, "include_answer": True},
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                snippet = (data.get("answer", "") + "\n" +
                           "\n".join(f"- {r.get('title','')}: {r.get('content','')[:200]}"
                                     for r in data.get("results", [])[:3]))
                results.append({"query": q, "findings": snippet[:800]})
        except Exception as e:
            print(f"Tavily error '{q}': {e}")
    return results


# ── Step 3: Client guide ──────────────────────────────────────────────────────
def generate_client_guide(topic, client_context, client_name,
                          transcript="", fathom_summary=""):
    """
    Returns richly structured guide data for docx rendering.
    Sections contain: heading, key_insight_box, action_steps, table (optional),
    key_takeaway, image_prompt, review_note.
    """
    brand_ctx = _get_brand_context(_GUIDE_VOICE_FILES, max_chars_each=3500)

    # Surface specific examples Elaine gave on the call
    transcript_excerpt = (transcript or fathom_summary or "")[:5000]

    # Detect which N2F mechanisms are actually mentioned in this call
    mechanism_check = ""
    for mech in ["Hormone-First Approach", "HormoneSync Movement",
                 "10-Hour Hormone Optimization Window", "Hormonal Driver Assessment"]:
        if mech.lower() in transcript_excerpt.lower():
            mechanism_check += f"\n- {mech} (mentioned in call — safe to reference)"
    if not mechanism_check:
        mechanism_check = "\nNone of the N2F proprietary mechanisms were explicitly mentioned in this call. Do NOT introduce any of them in this guide."

    prompt = f"""You are writing a client-facing guide for New To Fitness, Elaine Acheampong's PCOS hormone health coaching brand.

BRAND VOICE RULES (follow exactly):
{brand_ctx}

TOPIC: "{topic}"
CLIENT: {client_name}
CALL NOTES: {client_context}

CALL TRANSCRIPT (read carefully — every specific tip, food substitution, example, or actionable advice Elaine gave on this call MUST appear in the guide. Build the guide AROUND these specifics, then supplement with proven science):
{transcript_excerpt}

N2F MECHANISMS CLEARED FOR THIS GUIDE (only reference these — do not introduce any others):
{mechanism_check}

STRICT RULES:
1. The guide solves ONE specific problem: "{topic}"
2. Every section must have SPECIFIC ACTIONABLE STEPS — not "reduce oil" but "replace half your palm oil with light olive oil — same flavour profile, cuts the saturated fat load by 60%"
3. Extract every specific example from the transcript above and use them as the core content
4. Do NOT introduce broad coaching topics not directly related to {topic}
5. Banned phrases: "culturally intelligent", "culturally aware", "culturally sensitive", "hormone journey", "health journey", "empower yourself", "take control of your health"
6. Tone: Confident Yet Soft — warm, direct, specific. Write like a knowledgeable best friend, not a textbook
7. Evergreen: works for any client with cultural food + PCOS needs, not just {client_name}
8. Tables: use them to compare ingredient options, swap lists, or portion guides — wherever a table communicates information more clearly than prose
9. image_prompt: write a specific Gemini AI image generation prompt for a realistic, editorial-quality photo relevant to each section

Return ONLY valid JSON with no markdown fences. All string values must use \\n for line breaks, never literal newlines:
{{
  "guide_name": "Short outcome-first name following N2F naming conventions",
  "subtitle": "Specific outcome subheader — what they will achieve",
  "problem_statement": "One sentence: the exact problem this guide solves",
  "intro": "2-3 sentences max. Outcome-first. What they will be able to do after reading this. Warm and direct.",
  "sections": [
    {{
      "heading": "Section heading",
      "key_insight_box": "1-2 sentence key insight to call out visually — the most important thing to know in this section",
      "action_steps": ["Specific step with exact amounts/ratios/methods where relevant", "Another specific step"],
      "table": {{
        "use": false,
        "headers": [],
        "rows": []
      }},
      "key_takeaway": "One sentence: what to remember and implement from this section",
      "image_prompt": "Gemini AI prompt: realistic editorial wellness photo of [specific scene relevant to this section, e.g. 'a West African woman in a modern kitchen mixing palm oil and olive oil, warm natural lighting, editorial magazine aesthetic']",
      "review_note": "What Elaine should verify scientifically or update for brand voice — empty string if confident"
    }}
  ],
  "quick_reference_checklist": ["First actionable item from guide", "Second actionable item", "Continue for all key actions"],
  "mid_doc_cta": "Short warm CTA to appear mid-document — invite to the private WhatsApp community or next call (1-2 sentences)",
  "end_cta": "Closing CTA — invite to book a call, join community, and follow @ilaineyflex (2-3 sentences)",
  "note_to_send_to_client": "3-4 sentence warm message from Elaine to {client_name} referencing something specific from their call. Confident Yet Soft tone.",
  "keyword": "Specific automation keyword — NOT a common word clients say casually (avoid: food, cook, eat, oil, diet)"
}}

Include 4-6 sections. Use tables in at least 1-2 sections where swaps, comparisons, or ingredient lists are involved."""

    raw = _groq(prompt, temperature=0.4, max_tokens=5000)
    result = _parse_json(raw)
    if not result or not result.get("sections"):
        result = {
            "guide_name": f"{topic} Guide",
            "subtitle": "Actionable steps for hormonal balance",
            "problem_statement": f"How to manage {topic} with PCOS.",
            "intro": f"This guide covers {topic} for women managing PCOS.",
            "sections": [{"heading": "Overview", "content": raw[:2000],
                          "key_insight_box": "", "action_steps": [],
                          "table": {"use": False, "headers": [], "rows": []},
                          "key_takeaway": "", "image_prompt": "", "review_note": "Full review needed"}],
            "quick_reference_checklist": [],
            "mid_doc_cta": "Join our private WhatsApp community for ongoing support.",
            "end_cta": "Ready to go deeper? Book your next call with Elaine at newtofitness.com.",
            "note_to_send_to_client": f"Hi {client_name}, here is the guide we discussed.",
            "keyword": "cultural food guide",
        }
    return result


# ── Step 4: Selling opportunities ─────────────────────────────────────────────
def generate_selling_opportunities_content(guide_data, topic, search_results):
    brand_ctx = _get_brand_context(_SELLING_FILES, max_chars_each=3500)
    guide_name = guide_data.get("guide_name", topic)
    search_text = ("\n".join(f"[Search: {r['query']}]\n{r['findings']}"
                             for r in search_results)
                   if search_results else "No live search data.")

    prompt = f"""You are a digital product strategist for New To Fitness (N2F), Elaine Acheampong's premium PCOS coaching brand.

BRAND GUIDANCE:
{brand_ctx}

GUIDE CREATED:
Name: {guide_name}
Topic: {topic}

MARKET RESEARCH:
{search_text}

CRITICAL CONSTRAINTS:
- Digital products are LOW-TICKET DOWNLOADABLE GUIDES: $10-$47 price range ONLY
- Each product solves ONE SPECIFIC PROBLEM and delivers a quick win
- They are NOT coaching programs, NOT courses, NOT broad lifestyle guides
- Think: a beautiful 10-20 page PDF guide someone downloads, reads in 30 min, and implements today
- The guide must be complete enough to deliver a real result on its own

PRODUCT NAMING RULES (from N2F naming guide):
- Lead with transformation, not topic
- Premium buyer language — outcome-first
- No clinical jargon, no generic wellness phrases
- Title: what you do. Subtitle: what you get.

LEAD MAGNET RULES:
- Free, shorter version that delivers a quick win but creates desire for the paid guide
- Does NOT share a name with any top-5 upsell product (never give away for free what you charge for)
- Designed to pull people into our private WhatsApp community or social media

Return ONLY valid JSON with no markdown fences:
{{
  "upsell_products": [
    {{
      "rank": 1,
      "name": "Transformation-first product name",
      "subheader": "Outcome-focused subheader",
      "price_point": "$X (must be $10-$47)",
      "pages_estimate": "10-20 pages",
      "quick_win": "What result they get within 1-2 weeks",
      "score": 95,
      "reasoning": "Why this will sell based on market research"
    }}
  ],
  "lead_magnets": [
    {{
      "rank": 1,
      "name": "Lead magnet name",
      "subheader": "Benefit subheader",
      "format": "checklist/mini-guide/swap list/etc",
      "platform": "Instagram/TikTok/Both",
      "engagement_prediction": "Why this performs well on that platform"
    }}
  ],
  "carousel_scripts": [
    {{
      "promotes": "product or lead magnet name",
      "caption": "Full Instagram caption: hook, body, CTA. Include hashtags.",
      "slides": ["Slide 1: Hook copy (5-10 words)", "Slide 2 copy", "Slide 3 copy", "Slide 4 copy", "Slide 5 copy", "Slide 6: CTA copy"]
    }}
  ],
  "automation_keyword": "Specific 2-3 word phrase unique to this guide topic"
}}

Include 3-5 upsell products, 3-5 lead magnets, 5 carousel scripts."""

    raw = _groq(prompt, temperature=0.5, max_tokens=5000)
    result = _parse_json(raw)
    if not isinstance(result, dict):
        result = {"upsell_products": [], "lead_magnets": [],
                  "carousel_scripts": [], "automation_keyword": ""}
    return result


# ── Step 5: Digital product ───────────────────────────────────────────────────
def generate_digital_product_content(guide_data, selling_data,
                                     transcript="", fathom_summary=""):
    brand_ctx = _get_brand_context(_GUIDE_VOICE_FILES, max_chars_each=3000)
    upsells = selling_data.get("upsell_products", [])
    top = upsells[0] if upsells else {}
    product_name = top.get("name", guide_data.get("guide_name", ""))
    subheader    = top.get("subheader", "")
    price_point  = top.get("price_point", "$27")
    quick_win    = top.get("quick_win", "")
    topic        = guide_data.get("guide_name", "")
    transcript_excerpt = (transcript or fathom_summary or "")[:3000]

    prompt = f"""You are creating a LOW-TICKET DIGITAL PRODUCT for New To Fitness by Elaine Acheampong.

BRAND VOICE:
{brand_ctx}

PRODUCT:
Name: {product_name}
Subheader: {subheader}
Price: {price_point}
Quick win: {quick_win}
Topic: {topic}

CALL CONTENT TO DRAW FROM (use specific examples from this call as the foundation):
{transcript_excerpt}

CRITICAL RULES:
- This is a downloadable PDF guide, $10-47, 10-20 pages
- ONE specific problem, ONE clear transformation, ONE quick win
- NOT a coaching program. NOT a course. NOT broad lifestyle advice.
- Every section needs SPECIFIC ACTIONABLE STEPS with exact methods/ratios/timing
- Significantly more detailed than the free client guide but still focused on the same ONE topic
- Include at least one swap table or comparison table
- Include a simple 7-day implementation checklist (not a complex plan)
- Sections should have: key insight callout, action steps, table where relevant

BANNED: "empower", "journey", "holistic", "culturally intelligent", broad nutrition advice unrelated to the specific topic

Return ONLY valid JSON with no markdown fences:
{{
  "product_name": "{product_name}",
  "subheader": "{subheader}",
  "price_point": "{price_point}",
  "quick_win": "{quick_win}",
  "social_proof_placeholder": "Description of the type of before/after story that would work here (e.g. 'Client who kept eating jollof rice and still lost X lbs in Y weeks')",
  "intro": "2-3 sentence outcome-first welcome. Warm and direct.",
  "sections": [
    {{
      "heading": "Section heading",
      "key_insight_box": "The most important insight from this section in 1-2 sentences",
      "content": "2-3 paragraphs of specific, actionable content",
      "action_steps": ["Specific step 1", "Specific step 2"],
      "table": {{
        "use": false,
        "caption": "",
        "headers": [],
        "rows": []
      }},
      "image_prompt": "Gemini AI image prompt: realistic editorial photo of [specific scene]",
      "review_note": "What Elaine should verify — empty if confident"
    }}
  ],
  "seven_day_checklist": [
    {{"day": "Day 1-2", "action": "Specific action to take"}},
    {{"day": "Day 3-4", "action": "Next action"}},
    {{"day": "Day 5-7", "action": "Final action"}}
  ],
  "mid_cta": "Mid-document CTA: invite to private WhatsApp community (2 sentences)",
  "end_cta": "Closing CTA: book a discovery call + follow @ilaineyflex + join community (3 sentences, warm and direct)"
}}"""

    raw = _groq(prompt, temperature=0.4, max_tokens=6000)
    result = _parse_json(raw)
    if not isinstance(result, dict) or not result.get("sections"):
        result = {
            "product_name": product_name, "subheader": subheader,
            "price_point": price_point, "quick_win": quick_win,
            "social_proof_placeholder": "Client before/after story",
            "intro": f"Welcome to {product_name}.",
            "sections": [{"heading": "Content", "content": raw[:3000],
                          "key_insight_box": "", "action_steps": [],
                          "table": {"use": False, "caption": "", "headers": [], "rows": []},
                          "image_prompt": "", "review_note": "Full review needed"}],
            "seven_day_checklist": [],
            "mid_cta": "Join our private WhatsApp community.",
            "end_cta": "Book your call at newtofitness.com."
        }
    return result


# ── Step 6: Lead magnet ───────────────────────────────────────────────────────
def generate_lead_magnet_content(guide_data, selling_data,
                                 transcript="", fathom_summary=""):
    brand_ctx = _get_brand_context(_GUIDE_VOICE_FILES, max_chars_each=2500)
    lead_magnets = selling_data.get("lead_magnets", [])
    top = lead_magnets[0] if lead_magnets else {}
    freebie_name = top.get("name", f"{guide_data.get('guide_name', 'Guide')} — Free Guide")
    subheader    = top.get("subheader", "")
    fmt          = top.get("format", "mini-guide")
    topic        = guide_data.get("guide_name", "")
    transcript_excerpt = (transcript or fathom_summary or "")[:2000]

    prompt = f"""You are creating a FREE LEAD MAGNET for New To Fitness by Elaine Acheampong.

BRAND VOICE:
{brand_ctx}

LEAD MAGNET:
Name: {freebie_name}
Subheader: {subheader}
Format: {fmt}
Topic: {topic}

CALL CONTENT TO DRAW FROM:
{transcript_excerpt}

RULES:
- Genuinely valuable: delivers one quick win the reader can implement TODAY
- Deliberately incomplete: creates desire for the full paid guide
- 3-4 sections maximum, 5-8 pages
- Must include at least one swap list or comparison table
- Every section has specific actionable steps, not theory
- Ends with clear CTAs: join private WhatsApp community + follow @ilaineyflex + tease the paid guide
- Banned phrases: same as above

Return ONLY valid JSON with no markdown fences:
{{
  "freebie_name": "{freebie_name}",
  "subheader": "{subheader}",
  "intro": "1-2 sentences: what they get and why it matters right now",
  "sections": [
    {{
      "heading": "Section heading",
      "key_insight_box": "Key insight to highlight",
      "action_steps": ["Specific step 1", "Specific step 2"],
      "table": {{
        "use": false,
        "caption": "",
        "headers": [],
        "rows": []
      }},
      "image_prompt": "Gemini AI image prompt for this section",
      "review_note": ""
    }}
  ],
  "quick_checklist": ["Check item 1", "Check item 2"],
  "cta_community": "Invite to join N2F private WhatsApp community (1-2 sentences, warm)",
  "cta_upgrade": "Tease the paid guide — what more they get and how to get it (1-2 sentences)",
  "cta_social": "Invite to follow @ilaineyflex for daily tips (1 sentence)"
}}"""

    raw = _groq(prompt, temperature=0.4, max_tokens=3500)
    result = _parse_json(raw)
    if not isinstance(result, dict) or not result.get("sections"):
        result = {
            "freebie_name": freebie_name, "subheader": subheader,
            "intro": f"Your free guide to {topic}.",
            "sections": [{"heading": "Content", "content": raw[:2000],
                          "key_insight_box": "", "action_steps": [],
                          "table": {"use": False, "caption": "", "headers": [], "rows": []},
                          "image_prompt": "", "review_note": "Full review needed"}],
            "quick_checklist": [],
            "cta_community": "Join our private WhatsApp community.",
            "cta_upgrade": "Ready for the full guide?",
            "cta_social": "Follow @ilaineyflex for daily tips."
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


def _set_cell_shading(cell, fill_hex):
    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'), 'clear')
    shd.set(qn('w:color'), 'auto')
    shd.set(qn('w:fill'), fill_hex.lstrip('#'))
    tcPr.append(shd)


def _set_para_border(para, side, color_hex, size='12', space='4'):
    pPr = para._p.get_or_add_pPr()
    pBdr = pPr.find(qn('w:pBdr'))
    if pBdr is None:
        pBdr = OxmlElement('w:pBdr')
        pPr.append(pBdr)
    el = OxmlElement(f'w:{side}')
    el.set(qn('w:val'), 'single')
    el.set(qn('w:sz'), size)
    el.set(qn('w:space'), space)
    el.set(qn('w:color'), color_hex.lstrip('#'))
    pBdr.append(el)


def _gold_rule(doc):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after  = Pt(4)
    _set_para_border(p, 'bottom', 'D4A347', size='8')


def _setup_doc(doc):
    style = doc.styles['Normal']
    style.font.name  = 'Calibri'
    style.font.size  = Pt(11)
    style.font.color.rgb = CHOCOLATE
    for section in doc.sections:
        section.top_margin    = Inches(0.9)
        section.bottom_margin = Inches(0.9)
        section.left_margin   = Inches(1.1)
        section.right_margin  = Inches(1.1)


def _tmppath(prefix):
    fd, path = tempfile.mkstemp(suffix='.docx', prefix=prefix)
    os.close(fd)
    return path


# ── Reusable visual blocks ────────────────────────────────────────────────────

def _cover_page(doc, title, subtitle, tagline="New To Fitness | @ilaineyflex"):
    """Wine banner cover page."""
    # Top brand strip
    top = doc.add_paragraph()
    _set_shading(top, '7A0013')
    r = top.add_run(f"  {tagline}  ")
    r.font.size  = Pt(10)
    r.font.bold  = True
    r.font.color.rgb = GOLD
    top.paragraph_format.space_before = Pt(0)
    top.paragraph_format.space_after  = Pt(0)

    doc.add_paragraph()

    # Image placeholder
    img_box = doc.add_paragraph()
    _set_shading(img_box, 'F0E8D8')
    r = img_box.add_run(
        "\n[COVER IMAGE PLACEHOLDER]\n"
        "Gemini AI Prompt: \"Realistic editorial wellness photo of a woman preparing "
        "a vibrant African or South Asian meal in a modern kitchen, warm natural "
        "lighting, Vogue wellness aesthetic, N2F brand colors wine and gold accents\"\n"
        "Suggested size: full width, 3 inches tall\n"
    )
    r.font.size      = Pt(9)
    r.font.italic    = True
    r.font.color.rgb = RGBColor(0x7A, 0x6A, 0x5A)
    img_box.alignment = WD_ALIGN_PARAGRAPH.CENTER
    img_box.paragraph_format.space_after = Pt(12)

    # Title
    title_p = doc.add_paragraph()
    tr = title_p.add_run(title)
    tr.font.size  = Pt(26)
    tr.font.bold  = True
    tr.font.color.rgb = WINE
    title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title_p.paragraph_format.space_after = Pt(6)

    # Subtitle
    sub_p = doc.add_paragraph()
    sr = sub_p.add_run(subtitle)
    sr.font.size      = Pt(13)
    sr.font.italic    = True
    sr.font.color.rgb = CHOCOLATE
    sub_p.alignment   = WD_ALIGN_PARAGRAPH.CENTER
    sub_p.paragraph_format.space_after = Pt(20)

    _gold_rule(doc)
    doc.add_page_break()


def _section_heading(doc, heading):
    h = doc.add_paragraph()
    r = h.add_run(heading)
    r.font.size  = Pt(14)
    r.font.bold  = True
    r.font.color.rgb = WINE
    h.paragraph_format.space_before = Pt(16)
    h.paragraph_format.space_after  = Pt(4)
    _set_para_border(h, 'bottom', 'D4A347', size='4')


def _key_insight_box(doc, text):
    """Gold left-border insight callout."""
    if not text:
        return
    p = doc.add_paragraph()
    _set_shading(p, 'FBF5E6')
    _set_para_border(p, 'left', 'D4A347', size='24', space='8')
    r = p.add_run(f"Key Insight: {text}")
    r.font.size      = Pt(10.5)
    r.font.italic    = True
    r.font.color.rgb = CHOCOLATE
    p.paragraph_format.left_indent  = Inches(0.15)
    p.paragraph_format.space_before = Pt(6)
    p.paragraph_format.space_after  = Pt(8)


def _action_steps(doc, steps):
    """Formatted action step list with wine bullet markers."""
    if not steps:
        return
    label = doc.add_paragraph()
    lr = label.add_run("Action Steps")
    lr.font.size  = Pt(10)
    lr.font.bold  = True
    lr.font.color.rgb = WINE
    label.paragraph_format.space_before = Pt(8)
    label.paragraph_format.space_after  = Pt(2)

    for step in steps:
        p = doc.add_paragraph()
        r = p.add_run(f"•  {step}")
        r.font.size      = Pt(10.5)
        r.font.color.rgb = CHOCOLATE
        p.paragraph_format.left_indent   = Inches(0.2)
        p.paragraph_format.space_before  = Pt(2)
        p.paragraph_format.space_after   = Pt(3)


def _key_takeaway(doc, text):
    """Wine-background key takeaway box."""
    if not text:
        return
    p = doc.add_paragraph()
    _set_shading(p, 'F7E8E8')
    r = p.add_run(f"✨  Remember: {text}")
    r.font.size      = Pt(10.5)
    r.font.bold      = True
    r.font.color.rgb = WINE
    p.paragraph_format.space_before = Pt(6)
    p.paragraph_format.space_after  = Pt(10)


def _add_table(doc, headers, rows, caption=""):
    """Wine-header table."""
    if not headers and not rows:
        return
    n_cols  = max(len(headers), max((len(r) for r in rows), default=0)) if headers or rows else 2
    n_rows  = len(rows) + (1 if headers else 0)
    if n_cols == 0 or n_rows == 0:
        return

    if caption:
        cap = doc.add_paragraph()
        cr  = cap.add_run(caption)
        cr.font.size      = Pt(9)
        cr.font.italic    = True
        cr.font.color.rgb = GOLD
        cap.paragraph_format.space_before = Pt(8)
        cap.paragraph_format.space_after  = Pt(2)

    table = doc.add_table(rows=n_rows, cols=n_cols)
    table.style = 'Table Grid'
    row_i = 0

    if headers:
        for i, h_text in enumerate(headers[:n_cols]):
            cell = table.rows[0].cells[i]
            cell.text = h_text
            _set_cell_shading(cell, '7A0013')
            for para in cell.paragraphs:
                for run in para.runs:
                    run.font.bold      = True
                    run.font.color.rgb = WHITE
                    run.font.size      = Pt(10)
        row_i = 1

    for row_data in rows:
        for i, val in enumerate(row_data[:n_cols]):
            cell = table.rows[row_i].cells[i]
            cell.text = str(val)
            for para in cell.paragraphs:
                for run in para.runs:
                    run.font.size      = Pt(10)
                    run.font.color.rgb = CHOCOLATE
        row_i += 1

    doc.add_paragraph().paragraph_format.space_after = Pt(6)


def _image_placeholder(doc, gemini_prompt):
    """Gray placeholder box with Gemini image prompt."""
    if not gemini_prompt:
        return
    p = doc.add_paragraph()
    _set_shading(p, 'ECECEC')
    r = p.add_run(
        f"[IMAGE PLACEHOLDER]\n"
        f"Gemini AI Prompt: {gemini_prompt}\n"
        f"Suggested size: full width, 2.5 inches tall"
    )
    r.font.size      = Pt(8.5)
    r.font.italic    = True
    r.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(8)
    p.paragraph_format.space_after  = Pt(10)


def _cta_block(doc, text, style='mid'):
    """
    style='mid': gold background subtle CTA
    style='end': wine background bold closing CTA
    """
    doc.add_paragraph()
    p = doc.add_paragraph()
    fill = 'FBF5E6' if style == 'mid' else '7A0013'
    _set_shading(p, fill)
    _set_para_border(p, 'left', 'D4A347' if style == 'mid' else 'D4A347', size='20', space='8')
    r = p.add_run(text)
    r.font.size      = Pt(10.5)
    r.font.bold      = (style == 'end')
    r.font.color.rgb = CHOCOLATE if style == 'mid' else WHITE
    p.paragraph_format.left_indent  = Inches(0.15)
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after  = Pt(12)


def _checklist(doc, items, heading="Quick Reference Checklist"):
    if not items:
        return
    doc.add_paragraph()
    h = doc.add_paragraph()
    hr = h.add_run(heading)
    hr.font.size  = Pt(13)
    hr.font.bold  = True
    hr.font.color.rgb = WINE
    h.paragraph_format.space_before = Pt(12)
    h.paragraph_format.space_after  = Pt(6)
    _set_para_border(h, 'bottom', 'D4A347', size='4')

    for item in items:
        p = doc.add_paragraph()
        _set_shading(p, 'FAFAF5')
        r = p.add_run(f"☐  {item}")
        r.font.size      = Pt(10.5)
        r.font.color.rgb = CHOCOLATE
        p.paragraph_format.left_indent   = Inches(0.2)
        p.paragraph_format.space_before  = Pt(3)
        p.paragraph_format.space_after   = Pt(3)


def _social_proof_placeholder(doc, description):
    if not description:
        return
    doc.add_paragraph()
    h = doc.add_paragraph()
    hr = h.add_run("Client Success Story")
    hr.font.size  = Pt(12)
    hr.font.bold  = True
    hr.font.color.rgb = WINE

    p = doc.add_paragraph()
    _set_shading(p, 'F0F7EE')
    _set_para_border(p, 'left', '4A7A4A', size='20', space='8')
    r = p.add_run(
        f"[SOCIAL PROOF PLACEHOLDER]\n"
        f"{description}\n\n"
        f"Replace with a real client before/after story, screenshot, or testimonial.\n"
        f"Format: Photo (if available) + 2-3 sentence story + measurable result."
    )
    r.font.size      = Pt(9)
    r.font.italic    = True
    r.font.color.rgb = RGBColor(0x2A, 0x5A, 0x2A)
    p.paragraph_format.left_indent  = Inches(0.15)
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after  = Pt(12)


def _review_callout(doc, review_note):
    if not review_note:
        return
    p = doc.add_paragraph()
    _set_shading(p, 'FFF3CD')
    r = p.add_run(f"⚠ REVIEW FOR ELAINE: {review_note}")
    r.font.italic    = True
    r.font.size      = Pt(9)
    r.font.color.rgb = RGBColor(0x7A, 0x00, 0x13)
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after  = Pt(8)


def _claude_voice_prompt(doc):
    p = doc.add_paragraph()
    _set_shading(p, 'EDF4FB')
    r = p.add_run(
        "\U0001f4cb TO REFINE BRAND VOICE: Copy any section into Claude with: "
        "\"Rewrite for N2F Confident Yet Soft premium buyer voice. "
        "Ambitious woman with PCOS, wants outcomes not theory. "
        "Outcome-first language, no jargon, warm but authoritative. "
        "Reference N2F mechanisms only if part of her current program.\""
    )
    r.font.italic    = True
    r.font.size      = Pt(9)
    r.font.color.rgb = RGBColor(0x1E, 0x1A, 0x1A)
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after  = Pt(4)


def _footer_line(doc, text):
    p = doc.add_paragraph()
    r = p.add_run(text)
    r.font.size      = Pt(8)
    r.font.color.rgb = GOLD
    r.font.italic    = True
    p.alignment      = WD_ALIGN_PARAGRAPH.CENTER


# ── Docx builders ─────────────────────────────────────────────────────────────

def build_client_guide_docx(guide_data, client_name):
    doc = Document()
    _setup_doc(doc)

    # Cover
    _cover_page(doc,
                guide_data.get("guide_name", "Your Guide"),
                guide_data.get("subtitle", ""),
                "New To Fitness | @ilaineyflex")

    # Problem statement + intro
    ps = guide_data.get("problem_statement", "")
    if ps:
        p = doc.add_paragraph()
        _set_shading(p, 'F7E8E8')
        r = p.add_run(ps)
        r.font.size      = Pt(11)
        r.font.bold      = True
        r.font.color.rgb = WINE
        p.paragraph_format.space_after = Pt(8)

    intro = guide_data.get("intro", "")
    if intro:
        p = doc.add_paragraph(intro)
        p.paragraph_format.space_after = Pt(10)

    # Sections
    for i, s in enumerate(guide_data.get("sections", []), 1):
        _section_heading(doc, s.get("heading", ""))
        _key_insight_box(doc, s.get("key_insight_box", ""))
        _action_steps(doc, s.get("action_steps", []))

        tbl = s.get("table", {})
        if tbl.get("use") and tbl.get("headers"):
            _add_table(doc, tbl["headers"], tbl.get("rows", []),
                       caption=tbl.get("caption", ""))

        _key_takeaway(doc, s.get("key_takeaway", ""))
        _image_placeholder(doc, s.get("image_prompt", ""))
        _review_callout(doc, s.get("review_note", ""))

        # Mid-doc CTA after section 2
        if i == 2:
            _cta_block(doc, guide_data.get("mid_doc_cta", ""), style='mid')

    # Quick reference checklist
    _checklist(doc, guide_data.get("quick_reference_checklist", []))

    # Closing CTA
    _cta_block(doc, guide_data.get("end_cta", ""), style='end')

    _claude_voice_prompt(doc)
    doc.add_paragraph()
    _footer_line(doc, "New To Fitness | @ilaineyflex | Educational purposes only — not medical advice.")

    path = _tmppath('guide_')
    doc.save(path)
    return path


def build_selling_opportunities_docx(selling_data, guide_name):
    doc = Document()
    _setup_doc(doc)

    _cover_page(doc, f"{guide_name}", "Selling Opportunities Assessment",
                "New To Fitness — Internal Strategy Document")

    # Upsell products
    h = doc.add_paragraph()
    r = h.add_run("DIGITAL PRODUCT UPSELL OPPORTUNITIES")
    r.font.bold = True; r.font.size = Pt(14); r.font.color.rgb = WINE
    _gold_rule(doc)

    for prod in selling_data.get("upsell_products", []):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(14)
        r = p.add_run(f"#{prod.get('rank')}  {prod.get('name','')}")
        r.font.bold = True; r.font.size = Pt(13); r.font.color.rgb = WINE

        sub = doc.add_paragraph()
        sub.add_run(prod.get("subheader","")).font.italic = True

        meta_items = [
            f"Price: {prod.get('price_point','')}",
            f"Pages: {prod.get('pages_estimate','')}",
            f"Score: {prod.get('score','')}",
        ]
        meta = doc.add_paragraph()
        r = meta.add_run("  |  ".join(i for i in meta_items if i.split(': ')[1]))
        r.font.size = Pt(9.5); r.font.color.rgb = GOLD

        if prod.get("quick_win"):
            qw = doc.add_paragraph()
            _set_shading(qw, 'F0F7EE')
            r = qw.add_run(f"Quick win: {prod['quick_win']}")
            r.font.size = Pt(10); r.font.color.rgb = RGBColor(0x1A, 0x4A, 0x1A)

        reason = doc.add_paragraph(prod.get("reasoning",""))
        reason.paragraph_format.space_after = Pt(10)

    doc.add_paragraph()

    # Lead magnets
    h2 = doc.add_paragraph()
    r2 = h2.add_run("LEAD MAGNETS & FREEBIES")
    r2.font.bold = True; r2.font.size = Pt(14); r2.font.color.rgb = WINE
    _gold_rule(doc)

    for lm in selling_data.get("lead_magnets", []):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(12)
        r = p.add_run(f"#{lm.get('rank')}  {lm.get('name','')}")
        r.font.bold = True; r.font.size = Pt(12); r.font.color.rgb = CHOCOLATE

        sub = doc.add_paragraph()
        sub.add_run(lm.get("subheader","")).font.italic = True

        meta = doc.add_paragraph()
        r = meta.add_run(f"Format: {lm.get('format','')}  |  Platform: {lm.get('platform','')}")
        r.font.size = Pt(9.5); r.font.color.rgb = GOLD

        pred = doc.add_paragraph(lm.get("engagement_prediction",""))
        pred.paragraph_format.space_after = Pt(10)

    doc.add_paragraph()

    # Carousel scripts
    h3 = doc.add_paragraph()
    r3 = h3.add_run("CAROUSEL SCRIPTS (ready to use)")
    r3.font.bold = True; r3.font.size = Pt(14); r3.font.color.rgb = WINE
    _gold_rule(doc)

    for i, script in enumerate(selling_data.get("carousel_scripts", []), 1):
        cs = doc.add_paragraph()
        cs.paragraph_format.space_before = Pt(16)
        r = cs.add_run(f"CAROUSEL {i} — Promoting: {script.get('promotes','')}")
        r.font.bold = True; r.font.size = Pt(11)

        cap_lbl = doc.add_paragraph()
        r = cap_lbl.add_run("CAPTION:")
        r.font.bold = True; r.font.size = Pt(10); r.font.color.rgb = GOLD

        cap_p = doc.add_paragraph()
        _set_shading(cap_p, 'FAFAF0')
        cap_p.add_run(script.get("caption","")).font.size = Pt(10)
        cap_p.paragraph_format.space_after = Pt(6)

        slides_lbl = doc.add_paragraph()
        r = slides_lbl.add_run("SLIDE COPY:")
        r.font.bold = True; r.font.size = Pt(10); r.font.color.rgb = GOLD

        slides = script.get("slides", [])
        if slides:
            _add_table(doc,
                       ["Slide", "Copy"],
                       [[str(j+1), text] for j, text in enumerate(slides)])

    # Automation keyword
    doc.add_paragraph()
    kw = doc.add_paragraph()
    _set_shading(kw, 'F7E8E8')
    r = kw.add_run(f"AUTOMATION KEYWORD: {selling_data.get('automation_keyword','')}")
    r.font.bold = True; r.font.color.rgb = WINE

    path = _tmppath('selling_')
    doc.save(path)
    return path


def build_digital_product_docx(product_data):
    doc = Document()
    _setup_doc(doc)

    _cover_page(doc,
                product_data.get("product_name", "Guide"),
                product_data.get("subheader", ""),
                f"New To Fitness | @ilaineyflex  |  {product_data.get('price_point','')}")

    # Quick win promise
    qw = product_data.get("quick_win", "")
    if qw:
        p = doc.add_paragraph()
        _set_shading(p, 'F0F7EE')
        _set_para_border(p, 'left', '4A7A4A', size='20', space='8')
        r = p.add_run(f"Your Quick Win: {qw}")
        r.font.size = Pt(11); r.font.bold = True
        r.font.color.rgb = RGBColor(0x1A, 0x4A, 0x1A)
        p.paragraph_format.left_indent  = Inches(0.15)
        p.paragraph_format.space_after  = Pt(10)

    # Social proof placeholder
    _social_proof_placeholder(doc, product_data.get("social_proof_placeholder", ""))

    # Intro
    intro = product_data.get("intro", "")
    if intro:
        p = doc.add_paragraph(intro)
        p.paragraph_format.space_after = Pt(10)

    # Sections
    for i, s in enumerate(product_data.get("sections", []), 1):
        _section_heading(doc, s.get("heading", ""))
        _key_insight_box(doc, s.get("key_insight_box", ""))

        content = s.get("content", "")
        for line in content.split('\n'):
            if line.strip():
                p = doc.add_paragraph(line.strip())
                p.paragraph_format.space_after = Pt(5)

        _action_steps(doc, s.get("action_steps", []))

        tbl = s.get("table", {})
        if tbl.get("use") and tbl.get("headers"):
            _add_table(doc, tbl["headers"], tbl.get("rows", []),
                       caption=tbl.get("caption", ""))

        _image_placeholder(doc, s.get("image_prompt", ""))
        _review_callout(doc, s.get("review_note", ""))

        if i == 2:
            _cta_block(doc, product_data.get("mid_cta", ""), style='mid')

    # 7-day checklist
    checklist = product_data.get("seven_day_checklist", [])
    if checklist:
        doc.add_paragraph()
        ch = doc.add_paragraph()
        r = ch.add_run("Your 7-Day Implementation Plan")
        r.font.size = Pt(13); r.font.bold = True; r.font.color.rgb = WINE
        ch.paragraph_format.space_before = Pt(12)
        _set_para_border(ch, 'bottom', 'D4A347', size='4')

        _add_table(doc,
                   ["Timeframe", "Your Action"],
                   [[item.get("day",""), item.get("action","")] for item in checklist])

    # Closing CTA
    _cta_block(doc, product_data.get("end_cta", ""), style='end')

    _claude_voice_prompt(doc)
    doc.add_paragraph()
    _footer_line(doc, "New To Fitness | @ilaineyflex | Educational purposes only — not medical advice.")

    path = _tmppath('digital_product_')
    doc.save(path)
    return path


def build_lead_magnet_docx(freebie_data):
    doc = Document()
    _setup_doc(doc)

    _cover_page(doc,
                freebie_data.get("freebie_name", "Free Guide"),
                freebie_data.get("subheader", ""),
                "New To Fitness | @ilaineyflex  |  FREE")

    intro = freebie_data.get("intro", "")
    if intro:
        p = doc.add_paragraph(intro)
        p.paragraph_format.space_after = Pt(10)

    for i, s in enumerate(freebie_data.get("sections", []), 1):
        _section_heading(doc, s.get("heading", ""))
        _key_insight_box(doc, s.get("key_insight_box", ""))
        _action_steps(doc, s.get("action_steps", []))

        tbl = s.get("table", {})
        if tbl.get("use") and tbl.get("headers"):
            _add_table(doc, tbl["headers"], tbl.get("rows", []),
                       caption=tbl.get("caption", ""))

        _image_placeholder(doc, s.get("image_prompt", ""))
        _review_callout(doc, s.get("review_note", ""))

    # Quick checklist
    _checklist(doc, freebie_data.get("quick_checklist", []),
               heading="Your Quick Action Checklist")

    # CTA section
    doc.add_paragraph()
    cta_h = doc.add_paragraph()
    r = cta_h.add_run("Ready for More?")
    r.font.size = Pt(14); r.font.bold = True; r.font.color.rgb = WINE
    _gold_rule(doc)

    comm = freebie_data.get("cta_community", "")
    upg  = freebie_data.get("cta_upgrade", "")
    soc  = freebie_data.get("cta_social", "")

    if comm:
        _cta_block(doc, f"\U0001f4ac  {comm}", style='mid')
    if upg:
        _cta_block(doc, f"⭐  {upg}", style='end')
    if soc:
        p = doc.add_paragraph()
        r = p.add_run(f"\U0001f4f7  {soc}")
        r.font.size = Pt(10.5); r.font.italic = True; r.font.color.rgb = CHOCOLATE

    _claude_voice_prompt(doc)
    doc.add_paragraph()
    _footer_line(doc, "New To Fitness | @ilaineyflex | Free to share — not medical advice.")

    path = _tmppath('lead_magnet_')
    doc.save(path)
    return path


# ── Slack file upload ──────────────────────────────────────────────────────────
def upload_file_to_slack(channel_id, thread_ts, file_path, filename, comment=""):
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

    with open(file_path, 'rb') as f:
        up = requests.post(data["upload_url"], data=f)
    if up.status_code not in (200, 204):
        print(f"Slack file upload failed: {up.status_code}")
        return False

    payload = {"files": [{"id": data["file_id"], "title": filename}],
               "channel_id": channel_id}
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
    print(f"Slack upload: ok={result.get('ok')} {result.get('error','')}")
    return result.get("ok", False)


# ── Full pipeline ──────────────────────────────────────────────────────────────
def run_guide_pipeline(client_name, topic, client_context, transcript, fathom_summary,
                       channel_id, thread_ts, projects_channel_id):
    print(f"\n=== Guide pipeline: {client_name} | {topic} ===")
    guide_path = selling_path = digital_path = lm_path = None
    try:
        # 1. Client guide
        print("Step 1: client guide...")
        guide_data  = generate_client_guide(topic, client_context, client_name,
                                            transcript, fathom_summary)
        guide_name  = guide_data.get("guide_name", topic)
        note        = guide_data.get("note_to_send_to_client", "")
        guide_path  = build_client_guide_docx(guide_data, client_name)
        safe        = re.sub(r'[^\w\s-]', '', guide_name).strip().replace(' ', '_')
        upload_file_to_slack(channel_id, thread_ts, guide_path, f"{safe}.docx",
                             f"*Note to send to client:*\n{note}")
        print(f"Client guide posted for {client_name}")

        # 2. Market research
        print("Step 2: market research...")
        search_results = run_guide_market_research(topic)

        # 3. Selling opportunities
        print("Step 3: selling opportunities...")
        selling_data  = generate_selling_opportunities_content(guide_data, topic, search_results)
        selling_path  = build_selling_opportunities_docx(selling_data, guide_name)

        # 4. Digital product
        print("Step 4: digital product...")
        digital_data  = generate_digital_product_content(guide_data, selling_data,
                                                         transcript, fathom_summary)
        digital_path  = build_digital_product_docx(digital_data)
        digital_name  = re.sub(r'[^\w\s-]', '', digital_data.get("product_name", guide_name)).strip().replace(' ', '_')

        # 5. Lead magnet
        print("Step 5: lead magnet...")
        lm_data  = generate_lead_magnet_content(guide_data, selling_data,
                                                transcript, fathom_summary)
        lm_path  = build_lead_magnet_docx(lm_data)
        lm_name  = re.sub(r'[^\w\s-]', '', lm_data.get("freebie_name", guide_name)).strip().replace(' ', '_')

        # 6. Post 3 docs to #projects-launches-summaries
        if projects_channel_id:
            keyword    = selling_data.get("automation_keyword", guide_data.get("keyword", ""))
            intro_text = (
                f"*Guide Packet: {guide_name}*\n"
                f"From {client_name}'s call | Topic: {topic}\n"
                f"Automation keyword: `{keyword}`\n"
                f"3 documents attached for review:"
            )
            hdr = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
            ir  = requests.post("https://slack.com/api/chat.postMessage",
                                headers=hdr,
                                json={"channel": projects_channel_id, "text": intro_text})
            its = ir.json().get("ts") if ir.json().get("ok") else None

            upload_file_to_slack(projects_channel_id, its, selling_path,
                                 f"{safe}_Selling_Opportunities.docx",
                                 "Selling Opportunities Assessment")
            upload_file_to_slack(projects_channel_id, its, digital_path,
                                 f"{digital_name}_Digital_Product.docx",
                                 "Digital Product (Full Version)")
            upload_file_to_slack(projects_channel_id, its, lm_path,
                                 f"{lm_name}_Lead_Magnet.docx",
                                 "Lead Magnet / Freebie Version")
            print("All 3 docs posted to #projects-launches-summaries")

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
