# Source Graph და token economy — აუდიტის ადვერსარიული გადამოწმება

- **საწყისი აუდიტი:** 2026-08-04, GLM-5.2 / GitHub Copilot
- **გადამოწმება:** 2026-08-05, მიმდინარე `agent/v0886-uncapped-economy`
- **მტკიცებულების წესი:** ჰიპოთეზა არ ითვლება ეკონომიად, სანამ frozen A/B ან provider-ის რეალური usage receipt არ ადასტურებს

## შეჯამება

საწყისმა აუდიტმა სწორად გამოკვეთა Source Graph-ის lexical სიზუსტის,
როუტინგის უცნობი ფასისა და retry/reviewer attribution-ის გასაძლიერებელი
ზედაპირები. თუმცა სამი ყველაზე მაღალი პრიორიტეტის რეკომენდაცია — local
tokenizer, conversation compaction და cross-session response cache — P0
დეფექტებად არ დასტურდება.

AIWorkHub-ის ეკონომიკის canonical authority არის provider-ის მიერ დაბრუნებული
რეალური usage: input, cached input, cache creation/write, visible output,
reasoning output და, როცა provider იძლევა, ფასი. ლოკალური tokenizer შეიძლება
გამოდგეს მხოლოდ წინასწარ რეგისტრირებული counterfactual estimate-ისთვის; ის ვერ
ჩაანაცვლებს რეალურ receipt-ს და განსხვავებული provider/model tokenization-ის
გამო თვითონაც შეფასებაა.

მიმდინარე მოქმედებები:

- უცნობი ფასი აღარ გარდაიქმნება გამოგონილ `$99/1K` prior-ად ან `$9,900`
  task estimate-ად;
- worker და independent reviewer usage ცალკე role-ებად იწერება;
- retry რაოდენობა, token-ები და ცნობილი/უცნობი ფასი ცალკე ეკონომიკურ
  ჭრილად ითვლება;
- Source Graph-ის lexical call precision რჩება benchmark-first
  გასაძლიერებელ მიმართულებად;
- ხელოვნური token/USD cap default-ად არ ემატება. არსებული limit მხოლოდ
  owner-ის explicit opt-in პოლიტიკაა.

## მოთხოვნების ადვერსარიული შეფასება

| საწყისი მოთხოვნა | ვერდიქტი | საფუძველი / სწორი მოქმედება |
|---|---|---|
| Provider-specific tokenizer არის P0 | **უარყოფილია როგორც runtime P0** | Provider usage უკვე ზუსტად აღირიცხება. Tokenizer საჭიროა მხოლოდ labeled estimate/counterfactual benchmark-ისთვის. |
| Rolling conversation compaction არის P0 | **არ არის დამტკიცებული** | AIWorkHub worker-ები task-scoped პროცესებია; provider CLI-ის შიდა turn lifecycle-ს provider მართავს. Quadratic-growth claim-ს live trace ან A/B არ ახლავს. ჯერ უნდა გაიზომოს. |
| Cross-session response cache არის P0 | **უარყოფილია როგორც token claim** | თუ cached პასუხი კვლავ მოდელს ეგზავნება, input token-ები არ მცირდება. Candidate worktree-ის შედეგის repo-scoped cache-მა შეიძლება stale ან cross-task state გააჟონოს. Source Graph index უკვე persistent SQLite-ია. |
| 64 KiB bundle cap უნდა გაიზარდოს model context-ის მიხედვით | **benchmark-first** | დიდი cap ავტომატურად მეტ სარგებელს არ ნიშნავს და შესაძლოა input გაზარდოს. 64 KiB structural safety boundary-ია, არა token-truth claim. ცვლილებას frozen paired task-ები სჭირდება. |
| Lexical C/C++/PHP call edges გასაძლიერებელია | **დადასტურებული მიმართულება** | `impact`/`trace` ხარისხი პირდაპირ არის დამოკიდებული call resolution-ზე. საჭიროა labeled precision/recall corpus და regression gate, არა დაუზუსტებელი parser rewrite. |
| `$99/1K` უცნობი cost prior | **დადასტურებული დეფექტი** | უცნობი ფასი გამოგონილ task estimate-ს ქმნიდა და routing-ს აბინძურებდა. Unknown ახლა რჩება `None`; observed-cost კანდიდატები unknown-cost კანდიდატებზე წინ ფასდება. |
| Reviewer cost attribution | **დადასტურებული დეფექტი** | მხოლოდ topic-ით ირიბი inference არასაკმარისი იყო. ახალი usage event ინახავს `role=worker|reviewer`; legacy rows ცალკე inferred-ად აღირიცხება. |
| Retry economics | **დადასტურებული telemetry gap** | canonical attempts აღარ იკარგება. ledger ახლა აჩვენებს retry records/rate/tokens/cost და accepted retried tasks-ს, მაგრამ მიზეზობრივ ეკონომიას არ აცხადებს. |
| Default token ან USD cap | **უარყოფილია** | task-ის საჭირო ხარჯი წინასწარ უცნობია. მიზანია waste-ის შემცირება focused context/edit/retry ოპტიმიზაციით; explicit owner cap რჩება უსაფრთხოების არჩევანად. |

## Source Graph-ის დადასტურებული მდგომარეობა

ძლიერი მხარეები:

- repository-scoped persistent SQLite index;
- bounded traversal და deterministic budgets;
- AST/semantic extractor იქ, სადაც ენა მხარს უჭერს;
- conservative evidence იქ, სადაც მხოლოდ lexical/file-level authority არის;
- incremental refresh, deletion/rename reconciliation და manager/worker
  read boundaries;
- `focus`, `slice`, `context`, `calls`, `trace`, `impact`, `testmap`,
  `coverage` და typed bundle-ები, რომლებიც მოდელს მთლიანი რეპოს ნაცვლად bounded
  სამუშაო ზედაპირს აწვდიან.

ღია, მაგრამ ჯერ გაუზომავი მიმართულებები:

1. C/C++/CUDA/PHP lexical call-edge precision/recall;
2. broad architectural query-ის hit quality noisy repositories-ზე;
3. per-mode end-to-end contribution: validated outcome-მდე tokens, time,
   retries და manager acceptance;
4. semantic edit + Source Graph ერთობლივი ეფექტი მრავალ task family-ზე.

Source Graph-ის response byte ratio არ უნდა გადაითარგმნოს token saving-ად.
`60.26x` მსგავსი მაჩვენებელი მხოლოდ structural payload ratio-ა, სანამ paired
provider usage არ დაამტკიცებს end-to-end სხვაობას.

## ეკონომიკის canonical საზომები

სისტემა უნდა აფიქსირებდეს:

- requested model და provider-ის მიერ რეალურად observed model;
- worker/reviewer role;
- input, cached input, cache creation/write;
- visible output და reasoning output;
- total tokens და known/unknown cost;
- task attempt identity და retry ordinal;
- terminal outcome და manager decision;
- Source Graph modes/receipts და bounded/unbounded reads;
- semantic edit vs whole-file/full-read გზა.

შემდეგი საჯარო დასკვნა დასაშვებია მხოლოდ frozen paired artifact-იდან, სადაც
ორივე variant-ს აქვს იგივე task family, base revision, model/adapter,
validation, acceptance და provider usage receipt.

## შესრულებული ფიქსების claim boundary

ახალი role/retry ჭრები არის **observability**, არა ავტომატურად დაზოგილი
ტოკენები. მაგალითად, retry token-ის არსებობა არ ამტკიცებს, რომ retry თავიდან
ასაცილებელი იყო ან რომ მან acceptance გამოიწვია. ასევე observed model-ის უფრო
იაფი ფასი არ ამტკიცებს, რომ ხარისხი იგივე იქნებოდა — ამისთვის paired routing
experiment არის საჭირო.

უცნობი ფასი ახლა პატიოსნად უცნობია. ეს დროებით ამცირებს routing-ის
„სრულყოფილებას“, მაგრამ გამორიცხავს გამოგონილ ეკონომიკურ რანჟირებას.

## შემდეგი სწორი ექსპერიმენტები

1. **Routing A/B:** ერთი frozen task family სხვადასხვა ხელმისაწვდომ მოდელზე;
   total validated cost, retries, time და manager acceptance.
2. **Source Graph mode ablation:** იგივე task injected bundle-ით და bounded
   exact-file baseline-ით; სრული provider receipt და validation.
3. **Semantic edit expansion:** არსებული pilot-ის გამეორება რამდენიმე ენასა
   და edit family-ზე; full-file rewrite output-ის წინააღმდეგ.
4. **Lexical call benchmark:** labeled C/C++/CUDA/PHP calls, precision/recall,
   impact completeness და false-edge rate.
5. **Retry root-cause split:** provider failure, contract/validation failure,
   stale context, read inefficiency და genuine semantic rework ცალკე კლასებად.

Tokenizer, compaction ან persistent response cache მხოლოდ მაშინ გადავა
implementation roadmap-ში, თუ ამ გაზომვებიდან კონკრეტული bottleneck და
უსაფრთხო invariant გამოვა.

## დასკვნა

AIWorkHub-ის სწორი მიმართულება არის `uncapped by default`, მაგრამ არა
`unobserved`. ოპტიმიზაცია უნდა ეყრდნობოდეს provider receipts-ს, bounded source
context-ს, focused semantic edits-ს, retry root causes-ს და accepted outcome-ის
სრულ ფასს. მიმდინარე ფიქსები სწორედ measurement truth-ს აძლიერებს; ისინი არ
აცხადებს დაუმტკიცებელ multiplier-ს ან savings-ს.
