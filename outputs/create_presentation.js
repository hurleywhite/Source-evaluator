const pptxgen = require("pptxgenjs");
const React = require("react");
const ReactDOMServer = require("react-dom/server");
const sharp = require("sharp");
const {
  FaSearch, FaCogs, FaFileAlt, FaShieldAlt, FaBalanceScale,
  FaExclamationTriangle, FaCheckCircle, FaTimesCircle, FaBan,
  FaHandPaper, FaArrowRight, FaLightbulb, FaChartBar,
  FaRocket, FaSyncAlt, FaPlug, FaGlobeAmericas
} = require("react-icons/fa");

function renderIconSvg(IconComponent, color = "#000000", size = 256) {
  return ReactDOMServer.renderToStaticMarkup(
    React.createElement(IconComponent, { color, size: String(size) })
  );
}

async function iconToBase64Png(IconComponent, color, size = 256) {
  const svg = renderIconSvg(IconComponent, color, size);
  const pngBuffer = await sharp(Buffer.from(svg)).png().toBuffer();
  return "image/png;base64," + pngBuffer.toString("base64");
}

// ── Color Palette ──
const NAVY      = "1A2332";
const DARK_NAVY = "0F1620";
const SLATE     = "2D3E50";
const GOLD      = "D4A843";
const LIGHT_GOLD = "E8C876";
const WHITE     = "FFFFFF";
const OFF_WHITE = "F5F3EF";
const LIGHT_GRAY = "E8E6E1";
const MED_GRAY  = "8C8C8C";
const BODY_TEXT = "3A3A3A";
const MUTED     = "4B5563";

// ── Helper: fresh shadow each call ──
const makeShadow = () => ({ type: "outer", blur: 8, offset: 3, angle: 135, color: "000000", opacity: 0.12 });

async function createPresentation() {
  let pres = new pptxgen();
  pres.layout = "LAYOUT_16x9";
  pres.author = "Human Rights Foundation";
  pres.title = "Source Evaluator Overview";

  // Pre-render icons
  const icons = {
    search:   await iconToBase64Png(FaSearch, "#" + GOLD),
    cogs:     await iconToBase64Png(FaCogs, "#" + GOLD),
    file:     await iconToBase64Png(FaFileAlt, "#" + GOLD),
    shield:   await iconToBase64Png(FaShieldAlt, "#" + GOLD),
    balance:  await iconToBase64Png(FaBalanceScale, "#" + GOLD),
    warning:  await iconToBase64Png(FaExclamationTriangle, "#" + GOLD),
    check:    await iconToBase64Png(FaCheckCircle, "#" + WHITE),
    times:    await iconToBase64Png(FaTimesCircle, "#" + WHITE),
    ban:      await iconToBase64Png(FaBan, "#" + WHITE),
    hand:     await iconToBase64Png(FaHandPaper, "#" + WHITE),
    arrow:    await iconToBase64Png(FaArrowRight, "#" + GOLD),
    bulb:     await iconToBase64Png(FaLightbulb, "#" + GOLD),
    chart:    await iconToBase64Png(FaChartBar, "#" + GOLD),
    rocket:   await iconToBase64Png(FaRocket, "#" + GOLD),
    sync:     await iconToBase64Png(FaSyncAlt, "#" + GOLD),
    plug:     await iconToBase64Png(FaPlug, "#" + GOLD),
    globe:    await iconToBase64Png(FaGlobeAmericas, "#" + GOLD),
    checkGold: await iconToBase64Png(FaCheckCircle, "#" + GOLD),
    warningWhite: await iconToBase64Png(FaExclamationTriangle, "#" + WHITE),
  };

  // ════════════════════════════════════════
  // SLIDE 1: Title
  // ════════════════════════════════════════
  let s1 = pres.addSlide();
  s1.background = { color: NAVY };

  // Gold accent line at top
  s1.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.06, fill: { color: GOLD } });

  // Title
  s1.addText("Source Evaluator", {
    x: 0.8, y: 1.4, w: 8.4, h: 1.2,
    fontSize: 44, fontFace: "Georgia", color: WHITE, bold: true, margin: 0
  });

  // Subtitle
  s1.addText("Automated Source Credibility Assessment\nfor Human Rights Documentation", {
    x: 0.8, y: 2.7, w: 8.4, h: 1.0,
    fontSize: 18, fontFace: "Calibri", color: LIGHT_GOLD, margin: 0
  });

  // Divider
  s1.addShape(pres.shapes.RECTANGLE, { x: 0.8, y: 4.0, w: 2.0, h: 0.04, fill: { color: GOLD } });

  // Footer
  s1.addText("Human Rights Foundation", {
    x: 0.8, y: 4.3, w: 8.4, h: 0.5,
    fontSize: 14, fontFace: "Calibri", color: LIGHT_GOLD, margin: 0
  });

  // ════════════════════════════════════════
  // SLIDE 2: The Challenge
  // ════════════════════════════════════════
  let s2 = pres.addSlide();
  s2.background = { color: OFF_WHITE };

  // Gold top bar
  s2.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.06, fill: { color: GOLD } });

  s2.addText("The Challenge", {
    x: 0.8, y: 0.4, w: 8.4, h: 0.7,
    fontSize: 32, fontFace: "Georgia", color: NAVY, bold: true, margin: 0
  });

  const challenges = [
    "Human rights documentation demands rigorous source evaluation",
    "Researchers must evaluate dozens or hundreds of sources per report",
    "Manual evaluation is time-consuming and inconsistent across teams",
    "High-stakes claims require the highest evidentiary standards",
    "No standardized, auditable process exists today"
  ];

  // Challenge cards (left column)
  challenges.forEach((text, i) => {
    const yPos = 1.4 + i * 0.78;
    // Card background
    s2.addShape(pres.shapes.RECTANGLE, {
      x: 0.8, y: yPos, w: 8.4, h: 0.64,
      fill: { color: WHITE }, shadow: makeShadow()
    });
    // Gold left accent
    s2.addShape(pres.shapes.RECTANGLE, {
      x: 0.8, y: yPos, w: 0.06, h: 0.64,
      fill: { color: GOLD }
    });
    // Text
    s2.addText(text, {
      x: 1.15, y: yPos, w: 7.8, h: 0.64,
      fontSize: 14, fontFace: "Calibri", color: BODY_TEXT, valign: "middle", margin: 0
    });
  });

  // ════════════════════════════════════════
  // SLIDE 3: What It Does
  // ════════════════════════════════════════
  let s3 = pres.addSlide();
  s3.background = { color: OFF_WHITE };
  s3.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.06, fill: { color: GOLD } });

  s3.addText("What the Source Evaluator Does", {
    x: 0.8, y: 0.4, w: 8.4, h: 0.7,
    fontSize: 32, fontFace: "Georgia", color: NAVY, bold: true, margin: 0
  });

  // Key question callout
  s3.addShape(pres.shapes.RECTANGLE, {
    x: 0.8, y: 1.3, w: 8.4, h: 0.8,
    fill: { color: NAVY }
  });
  s3.addText([
    { text: "Core Question:  ", options: { color: GOLD, bold: true } },
    { text: '"How can this source be used?"', options: { color: WHITE, italic: true } }
  ], {
    x: 1.1, y: 1.3, w: 7.8, h: 0.8,
    fontSize: 18, fontFace: "Calibri", valign: "middle", margin: 0
  });

  const features = [
    { icon: icons.balance, text: "Evaluates sources against a 10-criterion framework" },
    { icon: icons.shield, text: "Outputs actionable use-permission recommendations \u2014 not abstract scores" },
    { icon: icons.search, text: "Every determination is traceable and defensible with a full audit trail" },
    { icon: icons.cogs, text: "Processes ~100 sources in ~10 minutes" },
  ];

  features.forEach((f, i) => {
    const yPos = 2.45 + i * 0.75;
    s3.addShape(pres.shapes.RECTANGLE, {
      x: 0.8, y: yPos, w: 8.4, h: 0.62,
      fill: { color: WHITE }, shadow: makeShadow()
    });
    s3.addImage({ data: f.icon, x: 1.05, y: yPos + 0.13, w: 0.36, h: 0.36 });
    s3.addText(f.text, {
      x: 1.65, y: yPos, w: 7.3, h: 0.62,
      fontSize: 14, fontFace: "Calibri", color: BODY_TEXT, valign: "middle", margin: 0
    });
  });

  // ════════════════════════════════════════
  // SLIDE 4: How It Works
  // ════════════════════════════════════════
  let s4 = pres.addSlide();
  s4.background = { color: OFF_WHITE };
  s4.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.06, fill: { color: GOLD } });

  s4.addText("How It Works", {
    x: 0.8, y: 0.4, w: 8.4, h: 0.7,
    fontSize: 32, fontFace: "Georgia", color: NAVY, bold: true, margin: 0
  });

  const steps = [
    { num: "1", title: "INPUT", desc: "Provide a list of cited URLs\n(works cited file)", icon: icons.file },
    { num: "2", title: "PROCESS", desc: "Tool fetches content, analyzes\nagainst 10 criteria, applies AI\nreview for edge cases", icon: icons.cogs },
    { num: "3", title: "OUTPUT", desc: "Actionable report classifying\neach source with evidence-\nbacked reasoning", icon: icons.chart },
  ];

  steps.forEach((step, i) => {
    const xPos = 0.8 + i * 3.15;

    // Card
    s4.addShape(pres.shapes.RECTANGLE, {
      x: xPos, y: 1.5, w: 2.7, h: 3.2,
      fill: { color: WHITE }, shadow: makeShadow()
    });

    // Navy header bar
    s4.addShape(pres.shapes.RECTANGLE, {
      x: xPos, y: 1.5, w: 2.7, h: 0.8,
      fill: { color: NAVY }
    });

    // Step number circle
    s4.addShape(pres.shapes.OVAL, {
      x: xPos + 0.95, y: 1.6, w: 0.6, h: 0.6,
      fill: { color: GOLD }
    });
    s4.addText(step.num, {
      x: xPos + 0.95, y: 1.6, w: 0.6, h: 0.6,
      fontSize: 22, fontFace: "Georgia", color: NAVY, bold: true, align: "center", valign: "middle", margin: 0
    });

    // Icon
    s4.addImage({ data: step.icon, x: xPos + 1.0, y: 2.6, w: 0.5, h: 0.5 });

    // Title
    s4.addText(step.title, {
      x: xPos + 0.2, y: 3.25, w: 2.3, h: 0.4,
      fontSize: 14, fontFace: "Calibri", color: GOLD, bold: true, align: "center", charSpacing: 3, margin: 0
    });

    // Description
    s4.addText(step.desc, {
      x: xPos + 0.2, y: 3.65, w: 2.3, h: 0.9,
      fontSize: 12, fontFace: "Calibri", color: BODY_TEXT, align: "center", margin: 0
    });

    // Arrow between cards
    if (i < 2) {
      s4.addImage({ data: icons.arrow, x: xPos + 2.85, y: 2.85, w: 0.35, h: 0.35 });
    }
  });

  // ════════════════════════════════════════
  // SLIDE 5: Six Actionable Outcomes
  // ════════════════════════════════════════
  let s5 = pres.addSlide();
  s5.background = { color: OFF_WHITE };
  s5.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.06, fill: { color: GOLD } });

  s5.addText("Six Actionable Outcomes", {
    x: 0.8, y: 0.4, w: 8.4, h: 0.7,
    fontSize: 32, fontFace: "Georgia", color: NAVY, bold: true, margin: 0
  });

  const outcomes = [
    { label: "Preferred Evidence", desc: "Cite for factual claims with confidence", color: "1B7340" },
    { label: "Usable with Safeguards", desc: "Cite with supporting sources", color: "2D8659" },
    { label: "Context Only", desc: "Use for background, not factual support", color: "3B7CB8" },
    { label: "Narrative Only", desc: 'Cite as "X claims..." (state media, self-interest)', color: "C4880B" },
    { label: "Manual Retrieval", desc: "Human must access directly (paywalled)", color: "8B6914" },
    { label: "Do Not Use", desc: "Satire, forums, unreliable (excluded)", color: "B83B3B" },
  ];

  outcomes.forEach((o, i) => {
    const col = i % 2;
    const row = Math.floor(i / 2);
    const xPos = 0.8 + col * 4.4;
    const yPos = 1.3 + row * 1.35;

    // Card
    s5.addShape(pres.shapes.RECTANGLE, {
      x: xPos, y: yPos, w: 4.0, h: 1.15,
      fill: { color: WHITE }, shadow: makeShadow()
    });

    // Color left accent
    s5.addShape(pres.shapes.RECTANGLE, {
      x: xPos, y: yPos, w: 0.08, h: 1.15,
      fill: { color: o.color }
    });

    // Label
    s5.addText(o.label, {
      x: xPos + 0.3, y: yPos + 0.15, w: 3.5, h: 0.4,
      fontSize: 15, fontFace: "Calibri", color: o.color, bold: true, margin: 0
    });

    // Description
    s5.addText(o.desc, {
      x: xPos + 0.3, y: yPos + 0.55, w: 3.5, h: 0.45,
      fontSize: 12, fontFace: "Calibri", color: MUTED, margin: 0
    });
  });

  // ════════════════════════════════════════
  // SLIDE 6: What Makes This Different
  // ════════════════════════════════════════
  let s6 = pres.addSlide();
  s6.background = { color: OFF_WHITE };
  s6.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.06, fill: { color: GOLD } });

  s6.addText("What Makes This Different", {
    x: 0.8, y: 0.4, w: 8.4, h: 0.7,
    fontSize: 32, fontFace: "Georgia", color: NAVY, bold: true, margin: 0
  });

  const innovations = [
    {
      title: "Access \u2260 Credibility",
      desc: "A paywalled NYT article isn't unreliable \u2014 it just needs manual retrieval. The tool separates fetchability from credibility.",
      icon: icons.bulb
    },
    {
      title: "Proportionality",
      desc: "The same source can be valid for narrative use but not for factual claims. Context matters.",
      icon: icons.balance
    },
    {
      title: "Severity-Aware",
      desc: "Claims of systematic abuse (genocide, crimes against humanity) trigger additional evidence gates automatically.",
      icon: icons.warning
    },
    {
      title: "Source Type Nuance",
      desc: "Automatically distinguishes NGOs, state media, advocacy orgs, think tanks, and news outlets.",
      icon: icons.globe
    },
  ];

  innovations.forEach((item, i) => {
    const col = i % 2;
    const row = Math.floor(i / 2);
    const xPos = 0.8 + col * 4.4;
    const yPos = 1.3 + row * 2.0;

    // Card
    s6.addShape(pres.shapes.RECTANGLE, {
      x: xPos, y: yPos, w: 4.0, h: 1.75,
      fill: { color: WHITE }, shadow: makeShadow()
    });

    // Icon
    s6.addImage({ data: item.icon, x: xPos + 0.3, y: yPos + 0.25, w: 0.4, h: 0.4 });

    // Title
    s6.addText(item.title, {
      x: xPos + 0.9, y: yPos + 0.25, w: 2.8, h: 0.4,
      fontSize: 16, fontFace: "Calibri", color: NAVY, bold: true, valign: "middle", margin: 0
    });

    // Description
    s6.addText(item.desc, {
      x: xPos + 0.3, y: yPos + 0.8, w: 3.5, h: 0.8,
      fontSize: 12, fontFace: "Calibri", color: BODY_TEXT, margin: 0
    });
  });

  // ════════════════════════════════════════
  // SLIDE 7: Test Results (Chart)
  // ════════════════════════════════════════
  let s7 = pres.addSlide();
  s7.background = { color: OFF_WHITE };
  s7.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.06, fill: { color: GOLD } });

  s7.addText("Results: 100-Source Evaluation", {
    x: 0.8, y: 0.4, w: 8.4, h: 0.7,
    fontSize: 32, fontFace: "Georgia", color: NAVY, bold: true, margin: 0
  });

  // Pie chart
  s7.addChart(pres.charts.PIE, [{
    name: "Results",
    labels: ["Preferred Evidence", "Usable w/ Safeguards", "Manual Retrieval", "Narrative Only", "Context Only", "Do Not Use"],
    values: [35, 17, 25, 11, 7, 5]
  }], {
    x: 0.3, y: 1.2, w: 5.0, h: 4.0,
    showPercent: true,
    showTitle: false,
    showLegend: true,
    legendPos: "b",
    legendFontSize: 10,
    legendColor: BODY_TEXT,
    chartColors: ["1B7340", "2D8659", "8B6914", "C4880B", "3B7CB8", "B83B3B"],
    dataLabelColor: WHITE,
    dataLabelFontSize: 11,
    dataLabelFontBold: true,
  });

  // Key insight callout on the right
  s7.addShape(pres.shapes.RECTANGLE, {
    x: 5.8, y: 1.5, w: 3.8, h: 3.5,
    fill: { color: NAVY }
  });

  s7.addText("Key Insight", {
    x: 6.1, y: 1.7, w: 3.2, h: 0.4,
    fontSize: 14, fontFace: "Calibri", color: GOLD, bold: true, charSpacing: 2, margin: 0
  });

  s7.addText("52%", {
    x: 6.1, y: 2.2, w: 3.2, h: 1.0,
    fontSize: 60, fontFace: "Georgia", color: WHITE, bold: true, margin: 0
  });

  s7.addText("of sources suitable for\nevidentiary use", {
    x: 6.1, y: 3.2, w: 3.2, h: 0.6,
    fontSize: 16, fontFace: "Calibri", color: LIGHT_GOLD, margin: 0
  });

  s7.addText("25% require manual retrieval\n(paywalls, bot-blocks)\n\n5% excluded as unreliable\n(satire, forums)", {
    x: 6.1, y: 4.0, w: 3.2, h: 0.9,
    fontSize: 11, fontFace: "Calibri", color: LIGHT_GOLD, margin: 0
  });

  // ════════════════════════════════════════
  // SLIDE 8: Capabilities
  // ════════════════════════════════════════
  let s8 = pres.addSlide();
  s8.background = { color: OFF_WHITE };
  s8.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.06, fill: { color: GOLD } });

  s8.addText("Capabilities", {
    x: 0.8, y: 0.4, w: 8.4, h: 0.7,
    fontSize: 32, fontFace: "Georgia", color: NAVY, bold: true, margin: 0
  });

  const capabilities = [
    { title: "Defensible", desc: "Every decision traces to specific evidence in the source text", icon: icons.shield },
    { title: "Scalable", desc: "Evaluates 100 sources in approximately 10 minutes", icon: icons.chart },
    { title: "Auditable", desc: "Full JSON export with evidence quotes for every determination", icon: icons.file },
    { title: "Resilient", desc: "Works offline with heuristics or enhanced with AI review", icon: icons.cogs },
    { title: "Intelligent Detection", desc: "Automatically flags state media, satire, and self-interest sources", icon: icons.search },
  ];

  capabilities.forEach((cap, i) => {
    const yPos = 1.3 + i * 0.82;
    s8.addShape(pres.shapes.RECTANGLE, {
      x: 0.8, y: yPos, w: 8.4, h: 0.68,
      fill: { color: WHITE }, shadow: makeShadow()
    });
    // Gold left accent
    s8.addShape(pres.shapes.RECTANGLE, {
      x: 0.8, y: yPos, w: 0.06, h: 0.68,
      fill: { color: GOLD }
    });
    s8.addImage({ data: cap.icon, x: 1.05, y: yPos + 0.16, w: 0.36, h: 0.36 });
    s8.addText(cap.title, {
      x: 1.65, y: yPos + 0.05, w: 2.0, h: 0.3,
      fontSize: 14, fontFace: "Calibri", color: NAVY, bold: true, valign: "middle", margin: 0
    });
    s8.addText(cap.desc, {
      x: 1.65, y: yPos + 0.35, w: 7.3, h: 0.3,
      fontSize: 12, fontFace: "Calibri", color: MUTED, valign: "middle", margin: 0
    });
  });

  // ════════════════════════════════════════
  // SLIDE 9: Known Limitations
  // ════════════════════════════════════════
  let s9 = pres.addSlide();
  s9.background = { color: OFF_WHITE };
  s9.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.06, fill: { color: GOLD } });

  s9.addText("Known Limitations", {
    x: 0.8, y: 0.4, w: 8.4, h: 0.7,
    fontSize: 32, fontFace: "Georgia", color: NAVY, bold: true, margin: 0
  });

  s9.addText("Transparency about what this tool cannot do", {
    x: 0.8, y: 1.0, w: 8.4, h: 0.4,
    fontSize: 14, fontFace: "Calibri", color: MUTED, italic: true, margin: 0
  });

  const limitations = [
    { title: "Access Barriers", desc: "Cannot bypass paywalls or login walls (~25% of sources need manual retrieval)" },
    { title: "English-Optimized", desc: "Optimized for English-language sources; limited non-English analysis" },
    { title: "Not a Fact-Checker", desc: "Evaluates source quality, not factual accuracy \u2014 does not replace fact-checking" },
    { title: "Human Judgment", desc: "Cannot replace editorial judgment, subject-matter expertise, or relevance assessment" },
  ];

  limitations.forEach((lim, i) => {
    const yPos = 1.65 + i * 0.95;
    s9.addShape(pres.shapes.RECTANGLE, {
      x: 0.8, y: yPos, w: 8.4, h: 0.78,
      fill: { color: WHITE }, shadow: makeShadow()
    });
    // Amber left accent
    s9.addShape(pres.shapes.RECTANGLE, {
      x: 0.8, y: yPos, w: 0.06, h: 0.78,
      fill: { color: "C4880B" }
    });
    s9.addText(lim.title, {
      x: 1.15, y: yPos + 0.1, w: 7.8, h: 0.3,
      fontSize: 14, fontFace: "Calibri", color: NAVY, bold: true, margin: 0
    });
    s9.addText(lim.desc, {
      x: 1.15, y: yPos + 0.4, w: 7.8, h: 0.3,
      fontSize: 12, fontFace: "Calibri", color: MUTED, margin: 0
    });
  });

  // ════════════════════════════════════════
  // SLIDE 10: Next Steps
  // ════════════════════════════════════════
  let s10 = pres.addSlide();
  s10.background = { color: NAVY };
  s10.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.06, fill: { color: GOLD } });

  s10.addText("Next Steps", {
    x: 0.8, y: 0.4, w: 8.4, h: 0.7,
    fontSize: 32, fontFace: "Georgia", color: WHITE, bold: true, margin: 0
  });

  const nextSteps = [
    { icon: icons.checkGold, title: "Calibrate", desc: "Validate against human expert judgment with 50\u2013100 sources" },
    { icon: icons.sync, title: "Feedback Loop", desc: "Track researcher overrides to continuously improve accuracy" },
    { icon: icons.plug, title: "Integrate", desc: "Embed into existing HRF research workflows" },
    { icon: icons.globe, title: "Expand", desc: "Potential for multi-language support and additional source types" },
  ];

  nextSteps.forEach((step, i) => {
    const col = i % 2;
    const row = Math.floor(i / 2);
    const xPos = 0.8 + col * 4.4;
    const yPos = 1.4 + row * 1.85;

    s10.addShape(pres.shapes.RECTANGLE, {
      x: xPos, y: yPos, w: 4.0, h: 1.55,
      fill: { color: SLATE }
    });

    s10.addImage({ data: step.icon, x: xPos + 0.3, y: yPos + 0.25, w: 0.4, h: 0.4 });

    s10.addText(step.title, {
      x: xPos + 0.9, y: yPos + 0.25, w: 2.8, h: 0.4,
      fontSize: 16, fontFace: "Calibri", color: GOLD, bold: true, valign: "middle", margin: 0
    });

    s10.addText(step.desc, {
      x: xPos + 0.3, y: yPos + 0.8, w: 3.4, h: 0.6,
      fontSize: 13, fontFace: "Calibri", color: WHITE, margin: 0
    });
  });

  // Closing footer
  s10.addShape(pres.shapes.RECTANGLE, { x: 0, y: 5.0, w: 10, h: 0.625, fill: { color: DARK_NAVY } });
  s10.addText("Human Rights Foundation", {
    x: 0.8, y: 5.05, w: 5.0, h: 0.5,
    fontSize: 13, fontFace: "Calibri", color: LIGHT_GOLD, valign: "middle", margin: 0
  });
  // Gold bottom bar
  s10.addShape(pres.shapes.RECTANGLE, { x: 0, y: 5.565, w: 10, h: 0.06, fill: { color: GOLD } });

  // ── Write File ──
  const outputPath = "/Users/hurleywhite/Desktop/Client Workflow Automations/[HRF] Source-evaluator/outputs/HRF_Source_Evaluator_Overview.pptx";
  await pres.writeFile({ fileName: outputPath });
  console.log("Presentation saved to: " + outputPath);
}

createPresentation().catch(err => { console.error(err); process.exit(1); });
