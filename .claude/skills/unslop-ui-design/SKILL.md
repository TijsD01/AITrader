---
name: unslop-ui-design
description: Strict UI/UX design and frontend rules to eliminate generic AI design slop and build clean, production-ready interfaces. Use when designing UI, creating a landing page, building a component, reviewing frontend code, or styling with Tailwind/CSS (e.g. src/web/static/).
version: 1.0.0
triggers:
  - design UI
  - create landing page
  - build component
  - review frontend
  - tailwind styling
---

# UI Design System: Anti-Slop Guidelines

Deze skill dwingt een professionele, doelgerichte en verfijnde UI-standaard af. AI-gegenereerde interfaces vervallen vaak in generieke patronen (overtollige badges, willekeurige cursieve tekst, vage slogans, overbodige borders en neon-glows). Gebruik onderstaande 20 regels bij het ontwerpen, bouwen of refactoren van gebruikersinterfaces.

---

## 1. Typografie & Copywriting

1. **Unitalicize random words**
   - Vermijd willekeurig gecursiveerde woorden in headers of titels. Cursief leidt af en oogt als een generiek marketingtemplate.
2. **Mono only where necessary**
   - Beperk monospace fonts (`font-mono`) strikt tot daadwerkelijke codeblokken, terminalcommando's, hashes, API-keys en data-tabellen. Gebruik voor gewone body- en heading-tekst een schone sans-serif of serif.
3. **Make headlines specific**
   - Vervang abstracte AI-clichés (*"Unlock seamless synergies with next-gen intelligence"*) door concrete, waardegedreven copy (*"Cited answers from papers"*, *"An engineer that lives in your terminal"*).
4. **Wrap headlines properly**
   - Voorkom 'orphans' (één enkel woord op een nieuwe regel). Gebruik in CSS `text-wrap: balance` of `text-wrap: pretty` op koppen en subkoppen.
5. **Use tabular numbers (`tabular-nums`)**
   - Gebruik altijd `font-variant-numeric: tabular-nums` (Tailwind: `tabular-nums`) voor timers, financiële bedragen, tellers en tabellen, zodat cijfers gelijke breedtes behouden en de interface niet verspringt bij updates.

---

## 2. Visuele Structuur & Witruimte

6. **Delete filler badges**
   - Verwijder betekenisloze pill-badges boven titels (*"✨ The Future of Work ✨"*). Gebruik badges uitsluitend als ze functionele metadata tonen (zoals *"v2.4"* of *"Beta"*).
7. **Drop redundant labels**
   - Verwijder overbodige labels die voor de hand liggen. Toon direct de data wanneer het contextueel duidelijk is (bijvoorbeeld geen *"E-mailadres:"* boven een duidelijk e-mailveld).
8. **Group with spacing (Gestalt proximity)**
   - Groepeer gerelateerde elementen met witruimte (`gap-*`, `space-y-*`) in plaats van overal randen, lijnen of grijze achtergrondvlakken omheen te plaatsen.
9. **Show the actual product**
   - Gebruik echte UI-previews, codevoorbeelden of productmockups in plaats van abstracte 3D-vormen, zwevende geometrische bollen of nietszeggende illustraties.
10. **Enlarge the main feature**
    - Doorbreek eentonigheid in feature-grids (bento grids). Maak de belangrijkste functionaliteit visueel dominant door deze meer spanwijdte, grotere afmetingen of meer detail te geven.

---

## 3. Kleur, Contrast & Verfraaiing

11. **Add a deliberate accent colour**
    - Vermijd zielloze, puur monochrome zwart-wit interfaces. Kies één herkenbare, heldere accentkleur voor interactieve elementen, actieve statussen en visuele focus.
12. **Improve text contrast**
    - Waak voor te laag contrast. Gebruik geen vage lichtgrijze tekst op witte of donkere achtergronden. Houd minimaal rekening met WCAG AA (4.5:1 voor normale tekst).
13. **Remove unnecessary embellishments**
    - Verwijder decoratieve matrix-patronen, overdreven neon-glows, willekeurige gradients en glas-effecten (glassmorphism) tenzij ze een directe functionele rol spelen.
14. **Use a design system**
    - Hanteer een consistent palet aan tokens (Tailwind, Radix of shadcn/ui schalen). Werk niet met willekeurige hex-codes of arbitraire pixelgroottes.

---

## 4. Componentgeometrie & Optische Uitlijning

15. **Inner radius = Outer radius - Padding**
    - Bereken geneste afrondingen altijd met de wiskundige formule:
      $$\text{radius}_{\text{inner}} = \max(0, \, \text{radius}_{\text{outer}} - \text{padding})$$
    - Dit voorkomt onevenwichtige, knellende hoeken tussen kaarten en de knoppen of badges erin.
16. **Keep icons consistent**
    - Gebruik iconen uit één iconenset (bijv. Lucide of Heroicons). Zorg voor gelijke stroke-width (bijv. altijd `1.5px` of `2px`) en consistente bounding boxes (`20x20px` of `24x24px`).
17. **Align by eye, not just by pixels**
    - Houd rekening met optisch gewicht. Een afspeelicoon (driehoek) binnen een cirkel moet bijvoorbeeld optisch iets naar rechts worden gecorrigeerd; centreer niet puur wiskundig op het geometrische middelpunt.

---

## 5. Interactie & Feedback

18. **Emphasize the CTA (Call-to-Action)**
    - Zorg voor één duidelijke primaire knop die direct in het oog springt door hoog contrast en stevig kleurgebruik. Houd secundaire acties subtiel (ghost/outline).
19. **Add loading states**
    - Zorg bij asynchrone acties altijd voor skeletframes (`skeleton loader`), subtiele micro-spinners of 'optimistic UI'-updates in plaats van een bevriezend scherm.
20. **Animate useful feedback**
    - Beperk animaties tot functionele micro-interacties (bijv. succescoepels, statusovergangen, tooltips). Gebruik geen overdreven binnenkomende scroll-animaties of trage fade-ins die de gebruiker vertragen.

---

## Implementatie & Code Checklist

Controleer gegenereerde frontend-code op de volgende CSS- en Tailwind-standaarden:

```html
<!-- Voorbeeld: Juiste verhouding radius en optische rust -->
<div class="rounded-[20px] p-4 bg-neutral-900 border border-neutral-800">
  <!-- Inner radius = 20px - 16px (p-4) = 4px -->
  <div class="rounded-[4px] bg-neutral-950 p-3">
    <!-- Tabular nums voor dynamische data -->
    <span class="font-mono tabular-nums text-lg font-semibold text-emerald-400">
      $29.99
    </span>
  </div>

  <!-- Gebalanceerde kop zonder orphans -->
  <h2 class="text-xl font-bold tracking-tight text-white [text-wrap:balance] mt-3">
    Cited answers from papers
  </h2>

  <!-- Functionele CTA met duidelijke hierarchie -->
  <button class="mt-4 w-full rounded-md bg-emerald-500 py-2.5 px-4 text-sm font-medium text-black transition-colors hover:bg-emerald-400 active:scale-[0.98]">
    Get Started
  </button>
</div>
```
