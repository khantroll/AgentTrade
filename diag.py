import json
s=json.load(open("agent_state.json"))
for k in ["portfolio_value","cash","llm_mode","positions","orders","last_orders","buckets"]:
    v=s.get(k)
    print("\n==",k,"==")
    print(type(v))
    print(str(v)[:1000])