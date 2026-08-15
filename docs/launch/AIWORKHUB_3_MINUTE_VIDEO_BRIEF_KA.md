# AIWorkHub — 3-წუთიანი ვიდეოს დრაფტი

ეს არის ბლოგერისთვის გადასაცემი საწყისი მასალა, არა სიტყვასიტყვით სავალდებულო
სცენარი. ქრონომეტრაჟი გათვლილია დაახლოებით **2:50–3:05 წუთზე** მშვიდი,
ენერგიული ტემპით. ეკრანის მოკლე ტექსტები ინგლისურადაა დატოვებული, რათა
პროდუქტის საერთაშორისო ბრენდს დაემთხვეს; voice-over დრაფტი ქართულადაა.

## ვიდეოს ერთი მთავარი აზრი

> **AIWorkHub არის open-source, local-first control plane, რომელიც სხვადასხვა
> coding model-ს ერთ repository-scoped engineering სისტემად აერთიანებს:
> გეგმავს, აწვდის ზუსტ კონტექსტს, უშვებს იზოლირებულ workers-ს, აგროვებს
> მტკიცებულებებს და მხოლოდ შემოწმების შემდეგ აძლევს manager-ს ცვლილების
> მიღების უფლებას.**

## ქრონომეტრაჟი და ტექსტი

### 0:00–0:15 — Hook

**კადრი:** ნელი zoom ახალ hero plate-ზე; ცენტრში AIWorkHub-ის ლოგო და სათაური
ცალკე overlay-ად გამოჩნდეს.

**ეკრანზე:**

```text
AIWorkHub
One control plane for your coding models.
```

**Voice-over:**

> Coding model-ები ბევრია, მაგრამ რეალურ პროექტში მთავარი სირთულე მხოლოდ
> კოდის დაწერა აღარ არის. ვინ დაგეგმავს სამუშაოს, ვინ მისცემს სწორ კონტექსტს,
> ვინ შეამოწმებს შედეგს და ვინ დაიცავს repository-ს?

**Asset:** [`aiworkhub-video-hero.png`](../assets/video/aiworkhub-video-hero.png)

### 0:15–0:35 — პრობლემა

**კადრი:** სწრაფი montage — რამდენიმე chat window, ხელით გადატანილი context,
გაურკვეველი retries და ერთმანეთზე გადაფარული branches; ბოლოს ყველაფერი ერთ
control plane-ში იყრის თავს.

**ეკრანზე:**

```text
Less copy/paste.
Less blind retry.
One repository truth.
```

**Voice-over:**

> ჩვეულებრივ multi-model workflow-ში კონტექსტი იკარგება, task-ები ერთმანეთს
> ეჯახება, ძვირი მოდელი მარტივ საქმეს აკეთებს, ხოლო კარგი პასუხი ხშირად
> მტკიცებულების გარეშე პირდაპირ კოდში ხვდება. AIWorkHub ამ პროცესს repository-ს
> შიგნით, ერთ გამჭვირვალე კონტროლის ციკლად აწყობს.

### 0:35–0:58 — მთელი სისტემა ერთ კადრში

**კადრი:** სრული architecture diagram; კამერა მიყვება ხუთ მთავარ სვეტს.

**ეკრანზე:**

```text
Observe → Decide → Delegate → Verify → Promote → Learn
```

**Voice-over:**

> თითო Git repository დამოუკიდებელი AI engineering workspace-ია. NeedFix და
> Roadmap იღებს პრობლემებსა და მიზნებს. Task DAG აწყობს dependency-ებს და
> write-scope collision-ებს. Context intelligence პოულობს საჭირო ცოდნას.
> Workforce ირჩევს მზადმყოფ route-ს. Worker იზოლირებულ გარემოში მუშაობს,
> ხოლო assurance layer შედეგს ამოწმებს და manager-ს საბოლოო გადაწყვეტილებას
> უტოვებს.

**Asset:** [`aiworkhub-system-architecture.png`](../../site/assets/aiworkhub-system-architecture.png)

### 0:58–1:24 — Source Graph და კონტექსტი

**კადრი:** repository files → parallel lanes → semantic graph. Highlight მხოლოდ
შეცვლილი ფაილები; შემდეგ გადადით Session, AI Memory, KB და Context Graph-ის
ოთხ პატარა overlay card-ზე.

**ეკრანზე:**

```text
Source Graph
Incremental · Parallel · Repository-aware
```

**Voice-over:**

> AIWorkHub-ის Source Graph კოდს სტრუქტურულად ხედავს — ფაილებს, symbols-ს,
> imports-ს, calls-სა და tests-ს. Hash-based incremental refresh ხელახლა
> ამუშავებს მხოლოდ შეცვლილ წყაროებს, მძიმე indexing კი CPU-aware parallel
> lanes-ზე ნაწილდება. Session Manager ინახავს მიმდინარე მდგომარეობას, AI
> Memory — გამოცდილებას, KB — შეთანხმებულ ცოდნას, Manager Context Graph კი
> ძველი გადაწყვეტილების ზუსტ საუბრის კონტექსტს აბრუნებს.

**Asset:** [`aiworkhub-video-source-graph.png`](../assets/video/aiworkhub-video-source-graph.png)

### 1:24–1:49 — Multi-model workforce

**კადრი:** `Plan → Route → Execute` flow; მოდელების სახელები გამოჩნდეს როგორც
ცალკე მოძრავი chips: Codex, Claude, Copilot, DeepSeek, GLM. არ აჩვენოთ, თითქოს
ყველა route ყველა მანქანაზე ავტომატურად ხელმისაწვდომია.

**ეკრანზე:**

```text
Use each model where it fits best.
Readiness · Capability · Cost · Observed outcomes
```

**Voice-over:**

> Codex, Claude, Copilot, DeepSeek და GLM აქ ერთმანეთის შემცვლელი chat-ები კი
> არა, ერთი workforce-ის execution routes-ია. AIWorkHub runtime-ში ხედავს
> რომელი provider და model არის რეალურად ხელმისაწვდომი, ამოწმებს preflight-ს
> და task-ს capability-ს, readiness-ს, ფასსა და წინა შედეგებს უთავსებს.

**Asset:** [`02-engineering-loop.png`](../assets/product-hunt/02-engineering-loop.png)

### 1:49–2:19 — იზოლირებული execution და evidence-first review

**კადრი:** assurance loop plate. ცალკე overlay labels მიაბით სადგურებს:
`Task`, `Isolated worker`, `Semantic edit`, `Correctness`, `Security`,
`Code quality`, `Manager accept`.

**ეკრანზე:**

```text
Workers propose.
Evidence proves.
The manager promotes.
```

**Voice-over:**

> Worker მუშაობს retained isolated worktree-ში და წერს მხოლოდ task-ით
> დაშვებულ paths-ზე. Existing file-ისთვის semantic edit მცირე ზუსტ range-ს
> ცვლის — მთლიანი ფაილის ხელახლა გენერირება აუცილებელი აღარ არის. შემდეგ მოდის
> tests, validation, correctness, security და code-quality review. Diffs,
> hashes, logs და tool receipts ერთ sealed evidence packet-ში იყრის თავს.
> მხოლოდ ამის შემდეგ შეუძლია მიმდინარე manager-ს accept, rework ან reject.

**Asset:** [`aiworkhub-video-assurance-loop.png`](../assets/video/aiworkhub-video-assurance-loop.png)

### 2:19–2:39 — Dashboard და durable runtime

**კადრი:** რეალური dashboard recording ან screenshot; მსუბუქი pan მხოლოდ
KPI cards-ზე, task states-ზე, Review Inbox-ზე, Roadmap-ზე და Operations-ზე.

**ეკრანზე:**

```text
One retained engineering loop.
Tasks · Context · Evidence · Review · Telemetry
```

**Voice-over:**

> Dashboard აჩვენებს არა მხოლოდ active task-ს, არამედ routing truth-ს,
> Source Graph health-ს, callbacks-ს, storage-ს, review queue-ს და ზუსტ
> terminal states-ს. State repository-ს საკუთარ `.aiworkhub` დირექტორიაში
> რჩება; AIWorkHub cloud account ან ცალკე hosted control server საჭირო არ არის.

**Assets:**

- [`03-dashboard.png`](../assets/product-hunt/03-dashboard.png)
- [`aiworkhub-self-hosted-dashboard.png`](../assets/screenshots/aiworkhub-self-hosted-dashboard.png)
- [`aiworkhub-task-review-loop.gif`](../assets/demo/aiworkhub-task-review-loop.gif)

### 2:39–2:54 — რა არის live და რა მოდის შემდეგ

**კადრი:** architecture diagram-ზე live components teal-ით; planned boxes
წამით პულსირებს. Planned ფუნქციები მკაფიოდ მოინიშნოს როგორც **Next**, არა
როგორც უკვე shipped.

**ეკრანზე:**

```text
Live: repository control loop
Next: smarter automation and cross-platform hardening
```

**Voice-over:**

> ძირითადი repository control loop უკვე მუშაობს. შემდეგი მიმართულებებია
> NeedFix-ის ავტომატური closure და TTL cleanup, provider enable/disable
> controls, უფრო ფართო CPU-aware parallelism, richer JSON evidence
> visualization და quality-calibrated release automation.

### 2:54–3:00 — დასასრული / CTA

**კადრი:** logo, GitHub და VS Code Marketplace მისამართები; hero plate-ზე
ნელი fade-out.

**ეკრანზე:**

```text
Open source · Local first · Repository native
github.com/shrec/AIWorkHub
```

**Voice-over:**

> AIWorkHub open source-ია. დააყენე VS Code-ში, მიაბი repository და გადააქციე
> რამდენიმე coding model ერთ მართვად engineering სისტემად.

## ვიდეოში გამოსაყენებელი asset-ების მოკლე სია

| Asset | დანიშნულება | ფორმატი |
| --- | --- | --- |
| [`aiworkhub-video-hero.png`](../assets/video/aiworkhub-video-hero.png) | Intro, outro, thumbnail crop | 16:9 PNG |
| [`aiworkhub-video-source-graph.png`](../assets/video/aiworkhub-video-source-graph.png) | Source Graph / parallel indexing | 16:9 PNG |
| [`aiworkhub-video-assurance-loop.png`](../assets/video/aiworkhub-video-assurance-loop.png) | Worker → evidence → manager loop | 16:9 PNG |
| [`aiworkhub-system-architecture.png`](../../site/assets/aiworkhub-system-architecture.png) | სრული სისტემის რუკა | Wide PNG |
| [`01-control-plane.png`](../assets/product-hunt/01-control-plane.png) | Clean title card | 16:9 PNG |
| [`02-engineering-loop.png`](../assets/product-hunt/02-engineering-loop.png) | Deterministic process diagram | 16:9 PNG |
| [`03-dashboard.png`](../assets/product-hunt/03-dashboard.png) | Dashboard explainer | 16:9 PNG |
| [`aiworkhub-task-review-loop.gif`](../assets/demo/aiworkhub-task-review-loop.gif) | რეალური product motion | GIF |

## მონტაჟის სწრაფი წესები

- Export: `1920×1080`, 30 fps, H.264; keep important content inside 90% safe area.
- AI-generated plates გამოიყენეთ როგორც background/B-roll; ზუსტი ტექსტი ყოველთვის
  post-production overlay იყოს.
- სათაური: 56–72 px; section labels: 30–40 px; body overlay მაქსიმუმ ორი ხაზი.
- ძირითადი ფერები: teal/cyan; amber მხოლოდ authority/evidence-ისთვის; red მხოლოდ
  reject/rework signal-ისთვის.
- ერთი კადრი 2–5 წამი; architecture diagram-ზე გამოიყენეთ crop/pan, არა ერთბაშად
  მთელი წვრილი ტექსტის წაკითხვა.
- მოდელების availability-ზე თქვით **runtime-discovered**, არა “ყველა მოდელი
  ყველგან ხელმისაწვდომია”.
- არ გამოიყენოთ დაუდასტურებელი “X-ჯერ ნაკლები tokens/cost” claim. დასაშვები ზუსტი
  მაგალითია მხოლოდ structural edit shape: `531 replacement bytes vs 31,998
  file bytes`; ეს სრული provider-token savings არ არის.
- AIWorkHub არ “იღებს კონტროლს” manager-ისგან: worker proposes, evidence verifies,
  manager accepts.

## მოკლე აღწერა პოსტისათვის

> AIWorkHub is an open-source, local-first control plane for multi-model AI
> coding agents. It combines repository-scoped task planning, Source Graph
> intelligence, durable context, isolated workers, evidence-based quality
> review and manager-controlled promotion inside VS Code and MCP.

