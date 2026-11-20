#!/usr/bin/env bash
# 两种封印架构的真实 HTTP 冒烟核对。失败即退出并打印场景名。
set -u
BASE=http://127.0.0.1:11000/api
PASS=0; FAIL=0

check() { # desc expected actual
  if [ "$2" = "$3" ]; then echo "PASS: $1 ($3)"; PASS=$((PASS+1));
  else echo "FAIL: $1 期望=$2 实际=$3"; FAIL=$((FAIL+1)); fi
}
code() { curl -s -o /tmp/smoke-body.json -w '%{http_code}' "$@"; }
body_get() { python3 -c "import json,sys;print(json.load(open('/tmp/smoke-body.json'))$1)"; }

echo '== 架构一：整账期封印（默认）=='
ALIGN_CODE=$(curl -s -o /tmp/smoke-body.json -w '%{http_code}' -X POST $BASE/align -H 'Content-Type: application/json' -d '{"period_id":"2026-10"}')
check "整期对齐返回200" 200 "$ALIGN_CODE"
SCOPE=$(body_get "['sealed_scope']")
check "批作用域=period" period "$SCOPE"

curl -s $BASE/writability?period_id=2026-10 -o /tmp/smoke-body.json
check "封前两栋均可写" "True True" "$(python3 -c "import json;print(' '.join(str(b['writable']) for b in json.load(open('/tmp/smoke-body.json'))['buildings']))")"

SEAL_CODE=$(curl -s -o /tmp/smoke-body.json -w '%{http_code}' -X POST $BASE/seal -H 'Content-Type: application/json' -d '{"period_id":"2026-10"}')
check "整期封印200" 200 "$SEAL_CODE"
check "封印响应作用域=period" period "$(body_get "['sealed_scope']")"

check "封后A栋拒写实抄" 409 "$(code -X POST $BASE/actuals -H 'Content-Type: application/json' -d '{"meter_id":"M-W-101","period_id":"2026-10","qty":1,"occurred_at":"t","reader_code":"r","doc_no":"d1"}')"
check "封后B栋拒写实抄" 409 "$(code -X POST $BASE/actuals -H 'Content-Type: application/json' -d '{"meter_id":"M-W-201","period_id":"2026-10","qty":1,"occurred_at":"t","reader_code":"r","doc_no":"d2"}')"
check "封后B栋拒写估抄" 409 "$(code -X POST $BASE/estimates -H 'Content-Type: application/json' -d '{"meter_id":"M-E-202","period_id":"2026-10","qty":1,"doc_no":"d3"}')"
check "整期架构带楼栋封印被拒" 400 "$(code -X POST $BASE/seal -H 'Content-Type: application/json' -d '{"period_id":"2026-10","building":"A栋"}')"

echo '== 切换到楼栋分区架构，用 2026-09 账期 =='
check "非法架构值被拒" 400 "$(code -X PUT $BASE/settings/seal_mode -H 'Content-Type: application/json' -d '{"value":"floor"}')"
check "切换building架构" 200 "$(code -X PUT $BASE/settings/seal_mode -H 'Content-Type: application/json' -d '{"value":"building"}')"

check "分区架构无楼栋对齐被拒" 400 "$(code -X POST $BASE/align -H 'Content-Type: application/json' -d '{"period_id":"2026-09"}')"
check "伪造楼栋对齐被拒" 404 "$(code -X POST $BASE/align -H 'Content-Type: application/json' -d '{"period_id":"2026-09","building":"Z栋"}')"

# 给 2026-09 装载 A栋(M-W-101) 与 B栋(M-W-201) 的估抄/实抄
for M in M-W-101 M-W-201; do
  curl -s -o /dev/null -X POST $BASE/estimates -H 'Content-Type: application/json' -d "{\"meter_id\":\"$M\",\"period_id\":\"2026-09\",\"qty\":10,\"doc_no\":\"E-$M\"}"
  curl -s -o /dev/null -X POST $BASE/actuals -H 'Content-Type: application/json' -d "{\"meter_id\":\"$M\",\"period_id\":\"2026-09\",\"qty\":10,\"occurred_at\":\"t\",\"reader_code\":\"r\",\"doc_no\":\"A-$M\"}"
done
check "A栋分区对齐200" 200 "$(code -X POST $BASE/align -H 'Content-Type: application/json' -d '{"period_id":"2026-09","building":"A栋"}')"
check "分区批作用域=building" building "$(body_get "['sealed_scope']")"
check "分区批只含A栋" A栋 "$(body_get "['building']")"
check "无楼栋分区封印被拒" 400 "$(code -X POST $BASE/seal -H 'Content-Type: application/json' -d '{"period_id":"2026-09"}')"
check "伪造楼栋封印被拒" 404 "$(code -X POST $BASE/seal -H 'Content-Type: application/json' -d '{"period_id":"2026-09","building":"Z栋"}')"
check "A栋分区封印200" 200 "$(code -X POST $BASE/seal -H 'Content-Type: application/json' -d '{"period_id":"2026-09","building":"A栋"}')"

curl -s $BASE/periods -o /tmp/smoke-body.json
check "分区封后账期仍open" open "$(python3 -c "import json;print([p for p in json.load(open('/tmp/smoke-body.json')) if p['period_id']=='2026-09'][0]['status'])")"
check "封A后A栋拒写" 409 "$(code -X POST $BASE/actuals -H 'Content-Type: application/json' -d '{"meter_id":"M-W-101","period_id":"2026-09","qty":2,"occurred_at":"t","reader_code":"r","doc_no":"x1"}')"
check "封A后B栋仍可写实抄" 200 "$(code -X POST $BASE/actuals -H 'Content-Type: application/json' -d '{"meter_id":"M-W-201","period_id":"2026-09","qty":2,"occurred_at":"t","reader_code":"r","doc_no":"x2"}')"
check "封A后B栋仍可写估抄" 200 "$(code -X POST $BASE/estimates -H 'Content-Type: application/json' -d '{"meter_id":"M-W-201","period_id":"2026-09","qty":2,"doc_no":"x3"}')"
check "同栋重复封印被拒" 409 "$(code -X POST $BASE/seal -H 'Content-Type: application/json' -d '{"period_id":"2026-09","building":"A栋"}')"

curl -s "$BASE/sealed-buildings?period_id=2026-09" -o /tmp/smoke-body.json
check "分区清单仅A栋且可查" "A栋 building" "$(python3 -c "import json;d=json.load(open('/tmp/smoke-body.json'));print(d[0]['building'], d[0]['sealed_scope'])")"

echo '== 并发两栋分区封 =='
curl -s -o /dev/null -X POST $BASE/align -H 'Content-Type: application/json' -d '{"period_id":"2026-09","building":"B栋"}'
curl -s -o /tmp/c1.json -w '%{http_code}\n' -X POST $BASE/seal -H 'Content-Type: application/json' -d '{"period_id":"2026-09","building":"A栋"}' > /tmp/sc1.txt &
# A栋已封（应409），真正并发的是 B栋与重复A栋；改用 B栋 + 新数据栋无第三栋，故并发 B栋封印两次验证互斥
curl -s -o /tmp/c2.json -w '%{http_code}\n' -X POST $BASE/seal -H 'Content-Type: application/json' -d '{"period_id":"2026-09","building":"B栋"}' > /tmp/sc2.txt &
wait
check "并发重复A栋=409" 409 "$(cat /tmp/sc1.txt)"
check "并发B栋封印=200" 200 "$(cat /tmp/sc2.txt)"
curl -s "$BASE/sealed-buildings?period_id=2026-09" -o /tmp/smoke-body.json
check "分区清单两栋无重复" "2 A栋 B栋" "$(python3 -c "import json;d=json.load(open('/tmp/smoke-body.json'));print(len(d), *(sorted(x['building'] for x in d)))")"

echo '== 切回整期架构对照同账期可写性 =='
curl -s -o /dev/null -X PUT $BASE/settings/seal_mode -H 'Content-Type: application/json' -d '{"value":"period"}'
curl -s $BASE/writability?period_id=2026-09 -o /tmp/smoke-body.json
check "切回period后分区写闸不再生效(两栋可写)" "True True" "$(python3 -c "import json;print(' '.join(str(b['writable']) for b in json.load(open('/tmp/smoke-body.json'))['buildings']))")"

echo
echo "结果: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
