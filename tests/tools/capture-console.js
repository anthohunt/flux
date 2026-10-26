#!/usr/bin/env node
// Capture de la console flux (headless, sans playwright — chromium bundle de
// puppeteer). Reutilisable par toutes les taches qui doivent prouver un rendu
// visuel (C5, C6, C7, C8, C10...), pas seulement T1.
//
// Usage :
//   node capture-console.js --wf veille --run <nom-de-run> \
//     --out-page docs/captures/xxx-page.png \
//     [--out-recap docs/captures/xxx-recap.png] \
//     [--out-texte tests/reference/xxx.txt] \
//     [--base http://127.0.0.1:8901]
//
// --out-texte : deplie TOUS les <details> du recapitulatif et ecrit son
//   innerText (extraction exhaustive, pour la non-regression).
// --masquer <selecteur CSS> : ce qui correspond est cache AVANT l'extraction
//   texte (innerText ignore ce qui ne s'affiche pas). Sert a comparer un rendu
//   qui a gagne des colonnes a une reference prise sans elles (C7).
// --out-recap (sans --out-texte, ou en plus) : replie tout, ne rouvre QUE
//   #recap-det + UN bloc "sous-agents" + UN bloc "tours, un par un" — une
//   capture lisible, pas un mur de 90 panneaux ouverts.
const puppeteer = require('puppeteer');
const fs = require('fs');
const path = require('path');

function args() {
  const a = {};
  const argv = process.argv.slice(2);
  for (let i = 0; i < argv.length; i++) {
    if (argv[i].startsWith('--')) { a[argv[i].slice(2)] = argv[i + 1]; i++; }
  }
  return a;
}

(async () => {
  const a = args();
  const WF = a.wf || 'veille';
  const RUN = a.run;
  const BASE = a.base || process.env.FLUX_API || 'http://127.0.0.1:8901';
  if (!RUN) { console.error('FAIL --run est obligatoire'); process.exit(1); }

  const browser = await puppeteer.launch({ headless: 'new', args: ['--no-sandbox'] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1000 });
  page.on('pageerror', e => console.log('PAGEERROR>', e.message));

  await page.goto(`${BASE}/${WF}`, { waitUntil: 'networkidle0', timeout: 30000 });
  await page.waitForSelector('#runs', { timeout: 15000 });
  await new Promise(r => setTimeout(r, 300));

  await page.click(`#runs [data-ws="${RUN}"]`);
  await new Promise(r => setTimeout(r, 800));
  await page.waitForFunction(() => {
    const h1 = document.getElementById('wfname');
    return h1 && h1.textContent && h1.textContent.trim().length > 0;
  }, { timeout: 15000 });

  const recapDet = await page.$('#recap-det');
  if (recapDet) {
    await page.evaluate(() => { document.getElementById('recap-det').open = true; });
    await new Promise(r => setTimeout(r, 500));
    await page.waitForFunction(() => {
      const box = document.getElementById('recap-mes');
      return box && box.querySelector('table');
    }, { timeout: 20000 }).catch(() => console.log('WARN pas de table dans #recap-mes'));
  }

  if (a['out-texte']) {
    if (a.masquer) {
      await page.addStyleTag({ content: a.masquer + '{display:none !important}' });
      console.log('masque :', a.masquer);
    }
    await page.evaluate(() => {
      const box = document.getElementById('recap-mes');
      if (box) box.querySelectorAll('details').forEach(d => { d.open = true; });
    });
    await new Promise(r => setTimeout(r, 400));
    const texte = await page.evaluate(() => {
      const box = document.getElementById('recap-mes');
      return box ? box.innerText : '';
    });
    fs.mkdirSync(path.dirname(a['out-texte']), { recursive: true });
    fs.writeFileSync(a['out-texte'], texte.trim() + '\n');
    console.log('TEXTE ecrit :', a['out-texte'], '(', texte.length, 'car.)');
  }

  // --cliquer <selecteur> : un clic avant la capture — ouvrir le dialogue
  // « nouvelle run », « valider / corriger »… (C5, C6).
  if (a.cliquer) {
    const ok = await page.evaluate(sel => { const el = document.querySelector(sel); if (!el) return false; el.click(); return true; }, a.cliquer);
    console.log('clic :', a.cliquer, ok ? 'ok' : 'INTROUVABLE');
    await new Promise(r => setTimeout(r, 900));
  }
  if (a['out-page'] || a['out-recap']) {
    // repli general, puis reouverture ciblee : une capture lisible, pas
    // 90 panneaux ouverts en meme temps
    const cible = await page.evaluate(() => {
      const box = document.getElementById('recap-mes');
      if (!box) return {};
      const all = Array.from(box.querySelectorAll('details'));
      all.forEach(d => { d.open = false; });
      const sousAgent = all.find(d => /sous-agent/.test((d.querySelector('summary') || {}).textContent || ''));
      const tour = all.find(d => /tours, un par un/.test((d.querySelector('summary') || {}).textContent || ''));
      const ouvrirAvecParents = (el) => { let p = el; while (p && p !== box) { if (p.tagName === 'DETAILS') p.open = true; p = p.parentElement; } };
      if (sousAgent) ouvrirAvecParents(sousAgent);
      if (tour) ouvrirAvecParents(tour);
      return {
        sousAgentTitre: sousAgent ? sousAgent.querySelector('summary').textContent.trim() : null,
        tourTitre: tour ? tour.querySelector('summary').textContent.trim() : null,
      };
    });
    console.log('bloc sous-agent ouvert :', cible.sousAgentTitre);
    console.log('bloc tours ouvert :', cible.tourTitre);
    await new Promise(r => setTimeout(r, 500));

    if (a['out-page']) {
      fs.mkdirSync(path.dirname(a['out-page']), { recursive: true });
      await page.screenshot({ path: a['out-page'], fullPage: true });
      console.log('PAGE ecrite :', a['out-page']);
    }
    if (a['out-recap']) {
      const recapHandle = await page.$('#recap');
      if (recapHandle) {
        fs.mkdirSync(path.dirname(a['out-recap']), { recursive: true });
        await recapHandle.screenshot({ path: a['out-recap'] });
        console.log('RECAP ecrit :', a['out-recap']);
      }
    }
  }

  // Captures dediees d'UN bloc : C7 demande la preuve « un sous-agent deplie »
  // et « un tour par tour deplie ». Une capture par bloc, cadree sur le bloc.
  const blocs = [
    ['out-sousagents', 'sous-agent'],
    ['out-tours', 'tours, un par un'],
  ];
  for (const [cle, motif] of blocs) {
    if (!a[cle]) continue;
    const titre = await page.evaluate((m) => {
      const box = document.getElementById('recap-mes');
      if (!box) return null;
      const all = Array.from(box.querySelectorAll('details'));
      all.forEach(d => { d.open = false; d.removeAttribute('data-capture'); });
      const cible = all.find(d => new RegExp(m).test((d.querySelector('summary') || {}).textContent || ''));
      if (!cible) return null;
      let p = cible;
      while (p && p !== box) { if (p.tagName === 'DETAILS') p.open = true; p = p.parentElement; }
      cible.setAttribute('data-capture', 'cible');
      cible.scrollIntoView();
      return cible.querySelector('summary').textContent.trim();
    }, motif);
    if (!titre) { console.log('WARN aucun bloc', motif, '- pas de capture pour', cle); continue; }
    await new Promise(r => setTimeout(r, 500));
    const h = await page.$('[data-capture="cible"]');
    fs.mkdirSync(path.dirname(a[cle]), { recursive: true });
    await h.screenshot({ path: a[cle] });
    console.log('BLOC ecrit :', a[cle], '->', titre);
  }

  await browser.close();
})().catch(e => { console.error('FAIL', e); process.exit(1); });
