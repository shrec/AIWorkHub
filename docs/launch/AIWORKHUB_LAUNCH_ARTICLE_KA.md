# AIWorkHub — რეპოზიტორზე დაფუძნებული მართვის სისტემა AI coding agent-ებისთვის

> **მთავარი ბმული:** https://github.com/shrec/AIWorkHub
>
> **მოკლე აღწერა:** AIWorkHub არის ღია კოდის, local-first VS Code სისტემა,
> რომელიც სხვადასხვა AI მოდელზე ანაწილებს დეველოპმენტის ამოცანებს, ინარჩუნებს
> პროექტის კონტექსტს და ცვლილებას მხოლოდ მტკიცებულებებით შემოწმების შემდეგ
> იღებს.

AI coding ინსტრუმენტები სწრაფად ვითარდება, მაგრამ რამდენიმე მოდელის ერთ
პროექტში კოორდინაცია ჯერ კიდევ ხშირად ხელით კეთდება: დავალებას ჩატში ვაკოპირებთ,
კოდის ხე თავიდან იკითხება, worker ცვლილებას აკეთებს და შედეგს მხოლოდ იმიტომ
ვიღებთ, რომ მოდელმა „დასრულებულიაო“ თქვა. meanwhile, პროექტის კონტექსტი,
ლოგები და გადაწყვეტილებების ისტორია სხვადასხვა ადგილას იფანტება.

**AIWorkHub** სწორედ ამ პრობლემის გადასაჭრელად ავაშენე.

ეს არის open-source, local-first control plane მულტი-მოდელური პროგრამული
დეველოპმენტისთვის VS Code-ში. თითოეული Git რეპოზიტორი იღებს საკუთარ task
queue-ს, კოდის ინდექსს, ხანგრძლივ კონტექსტს, worker isolation-ს, evidence
bundle-ებს და manager review პროცესს. სისტემა იყენებს VS Code-ში ან შესაბამის
CLI-ში უკვე ავტორიზებულ მოდელებს და პროექტის მართვას AIWorkHub-ის cloud
სერვისზე არ გადააქვს.

პროექტი MIT ლიცენზიითაა ხელმისაწვდომი:

**https://github.com/shrec/AIWorkHub**

## მთავარი პრობლემა კოორდინაციაა და არა კიდევ ერთი ჩატი

როცა პროექტზე რამდენიმე coding agent მუშაობს, მთავარი კითხვები მხოლოდ კოდის
გენერაციას აღარ ეხება:

- რომელი task არის მზად და რომელი სხვა ცვლილებაზეა დამოკიდებული?
- რომელი მოდელი შეესაბამება ამოცანას და ამ წუთას ხელმისაწვდომია?
- რომელი ფაილების შეცვლის უფლება აქვს worker-ს?
- იყენებდა თუ არა მოდელი დამტკიცებულ Source Graph-ს მუშაობის მთელ პროცესში?
- რა diff, tests, logs და artifacts ამტკიცებს შედეგს?
- ვინ იღებს ცვლილებას და ვინ აახლებს პროექტის კანონიკურ მდგომარეობას?
- როგორ ბრუნდება callback ზუსტად იმ manager chat-ში და იმ რეპოზიტორში,
  საიდანაც task შეიქმნა?
- რა რჩება reload-ის, ხანგრძლივი სესიის ან context compaction-ის შემდეგ?

AIWorkHub ამ ყველაფერს deterministic control-plane პრობლემად განიხილავს.
მოდელი აზროვნებს და წერს კოდს; პროგრამული სისტემა კი მართავს identity-ს,
routing-ს, lifecycle-ს, isolation-ს და evidence-ს.

## თითოეულ რეპოზიტორს საკუთარი იზოლირებული სივრცე აქვს

ერთჯერადი explicit initialization-ის შემდეგ იქმნება `.aiworkhub/` დირექტორია.
Task-ები, callback ჩანაწერები, ინდექსები, sessions, memories, KB და audit
evidence მხოლოდ ამ რეპოზიტორს ეკუთვნის.

არ არსებობს საერთო project database ან AIWorkHub HTTP service. VS Code-ის
extension რეპოზიტორზე მიბმულ MCP runtime-ს stdio-თი უკავშირდება. ამიტომ ორ
VS Code ფანჯარაში გახსნილი ორი პროექტი ერთმანეთის task-ებსა და კონტექსტს
ჩუმად ვერ აურევს.

ამავე verified manager chat-ს შეუძლია live რეპოზიტორებს შორის stable
`repo_id`-ით გადართვა. იცვლება არჩეული პროექტი და არა manager thread-ის
იდენტობა.

## Task lifecycle რეალური review საზღვრით

AIWorkHub-ის task card აღწერს:

- მიზანსა და acceptance criteria-ს;
- runner-სა და model route-ს;
- დასაშვებ write paths-სა და აკრძალულ ქმედებებს;
- dependencies-სა და collision constraints-ს;
- required outputs-სა და validation commands-ს.

Worker-ები bounded workspace-ში მუშაობენ. პროცესის დასრულება ცვლილების
ავტომატურ მიღებას არ ნიშნავს: ნებისმიერი terminal outcome review-ში შედის
თავისი რეალური სტატუსითა და evidence-ით. Manager ამოწმებს diff-ს, tests-ს,
logs-ს, artifacts-ს, validation history-სა და tool-use receipts-ს, შემდეგ კი
ცვლილებას იღებს ან ზუსტ residual-ს აბრუნებს გადასამუშავებლად.

„Agent-მა მუშაობა დაასრულა“ და „რეპოზიტორი ცვლილებას იღებს“ ორი სხვადასხვა
მოვლენაა.

## Source Graph განმეორებითი tree scan-ის ნაცვლად

AIWorkHub აქტიური რეპოზიტორისთვის ავტომატურად განახლებად **Source Graph-ს**
ინარჩუნებს. Manager და worker კოდის ფართო filesystem scan-ის ნაცვლად bounded,
სტრუქტურულ კონტექსტს ითხოვენ.

მიზანი არ არის დაუდასტურებელი „X პროცენტით ნაკლები token“ მარკეტინგული
დაპირება. სისტემა ინახავს context evidence-ს: რა მოითხოვა მოდელმა, რა მიეწოდა,
რა დაადასტურა receipt-ით, რა მოიჭრა და რატომ გადავიდა route degraded რეჟიმში.
ასე context efficiency-ის გაზომვა შესაძლებელია bytes-ის tokens-ად ან dollars-ად
არასწორი გამოცხადების გარეშე.

Source Graph-ის გამოყენებაც task evidence-ის ნაწილია. შედეგად შეიძლება
გაიზომოს, იყენებდა თუ არა მოდელი კოდის ინტელექტის სწორ არხს მთელი task-ის
განმავლობაში და არა მხოლოდ prompt-ის დასაწყისში.

## ხანგრძლივ კონტექსტს რამდენიმე განსხვავებული ავტორიტეტი აქვს

AIWorkHub განზრახ გამოყოფს სხვადასხვა დანიშნულების კონტექსტურ სისტემას:

- **Session Manager** — მიმდინარე state, checkpoints და handoffs;
- **AI Memory** — მრავალჯერ გამოსაყენებელი გადაწყვეტილებები და lessons;
- **Knowledge Base** — დამტკიცებული project contracts და ფაქტები;
- **Source Graph** — bounded structural code intelligence;
- **Manager Context Graph** — optional, manager-only completed-chat evidence
  და deterministic კავშირები repository/thread/session/task/actor/event
  იდენტობებს შორის.

Context Graph recovery layer-ია და არა ავტომატური policy engine. ძველი
მიმოწერა შეიძლება გადაწყვეტილების მიზეზს ადასტურებდეს, მაგრამ მიმდინარე KB-ს
ან task contract-ს ჩუმად ვერ შეცვლის. ამ ეტაპზე passive capture მუშაობს Codex-ის
დასრულებულ user/assistant შეტყობინებებზე; reasoning, streaming deltas, tool
output, commands და approval prompts არ ინახება.

## რამდენიმე მოდელი credentials-ის კოპირების გარეშე

ლოკალურად არსებული შესაძლებლობების მიხედვით AIWorkHub-ს შეუძლია task-ების
გადანაწილება Codex, Claude, DeepSeek, GLM და VS Code Language Model API-ში
ხელმისაწვდომ მოდელებზე.

Editor route იყენებს VS Code-ში უკვე ხილულ მოდელს და editor-ის ჩვეულებრივ
ერთჯერად consent-ს. CLI route იყენებს იმავე CLI-ის ავტორიზებულ სესიას.
AIWorkHub არც editor-ის და არც CLI-ის credentials-ს რეპოზიტორში არ აკოპირებს.

რადგან subscriptions და model catalogs განსხვავდება, ხელმისაწვდომობა runtime-ზე
აღმოჩნდება. Preflight წინასწარ აჩვენებს, რომელი adapter არის დაყენებული,
ავტორიზებული და launch-ready.

## უსაფრთხოება არქიტექტურის ნაწილია

AIWorkHub-ში შედის:

- ცალ-ცალკე write და process-launch gates, ორივე default-off;
- shell-free exact-task launch;
- explicit allowed-write scopes;
- worker isolation და Landlock იქ, სადაც ხელმისაწვდომია;
- repository-ის გარეთ შენახული owner-only credentials;
- secret-redacted logs;
- durable callback delivery lease/retry/deduplication-ით;
- fail-closed repository/manager/task/claim identity checks;
- append-only audit evidence და authenticated tool-use receipts.

საბოლოო acceptance manager-ს ეკუთვნის. Worker-ს manager-ისგან ახალი worker-ების
გაშვების უფლება მემკვიდრეობით არ გადაეცემა.

## Dashboard

VS Code-ის editor tab-ში გახსნილი dashboard აჩვენებს task queue-ს,
dependencies-ს, review inbox-ს, live output-ს, callback health-ს, model
preflight-ს, storage usage-ს, Source Graph-ის მდგომარეობასა და repository
context stores-ს.

![AIWorkHub dashboard](../assets/screenshots/aiworkhub-self-hosted-dashboard.png)

AIWorkHub საკუთარი დეველოპმენტის სამართავადაც გამოიყენება: მისი task-ების
დაგეგმვა, worker-ებზე განაწილება და review თავად AIWorkHub-ის repository
instance-იდან შეიძლება.

## ინსტალაცია

ამჟამინდელი საჯარო distribution channel არის GitHub Release VSIX.
Marketplace, Open VSX და PyPI publication ჯერ live არ არის.

1. ჩამოტვირთეთ `aiworkhub-*.vsix`
   [ბოლო release-იდან](https://github.com/shrec/AIWorkHub/releases/latest).
2. დააინსტალირეთ:

   ```bash
   code --install-extension aiworkhub-*.vsix
   ```

3. VS Code-ში გახსენით Git რეპოზიტორი.
4. გაუშვით **AIWorkHub: Open Dashboard**.
5. ერთხელ აირჩიეთ **Initialize AIWorkHub**.
6. გახსენით ახალი model chat, რათა repository MCP tools აღმოაჩინოს.

Packaged extension qualified არის Linux, macOS, native Windows, WSL და
Remote-SSH გარემოებისთვის. Initialization explicit და idempotent-ია: ქმნის
local stores-ს და იწყებს Source Graph-ის პირველ ინდექსირებას.

## შემდეგი ნაბიჯები

საინჟინრო საფუძველი უკვე მუშაობს, თუმცა ეს ჯერ ადრეული open-source release-ია.
შემდეგი ეტაპებია distribution-ის გაფართოება, provider-independent callback
qualification, უკეთესი visual planning, historical reliability analytics და
context economics-ის უფრო მარტივად დათვალიერება.

თუ ეს მიმართულება თქვენთვის საინტერესოა, გამოცადეთ release არასაკრიტიკულ
რეპოზიტორზე, autonomous launch-ის ჩართვამდე წაიკითხეთ security model და
გაგვიზიარეთ კონკრეტული friction point-ები.

**Repository:** https://github.com/shrec/AIWorkHub  
**Releases:** https://github.com/shrec/AIWorkHub/releases  
**License:** MIT

მადლობა [null0xxx-ს](https://github.com/null0xxx)
[kimi-atlas-ის](https://github.com/null0xxx/kimi-atlas) გაზიარებისა და
multi-agent orchestration/evidence-driven verification-ზე საინტერესო
იდეებისთვის.

