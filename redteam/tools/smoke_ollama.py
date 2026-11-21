"""Smoke-test an ollama model through the OpenAI endpoint: can it call a
BitSwarm tool? Usage: python redteam/tools/smoke_ollama.py <ollama-model>"""
import sys
from openai import OpenAI
sys.path.insert(0, '.')
from miner.agent_openai import _to_openai_tools
from miner.tools import TOOL_DEFINITIONS

model = sys.argv[1] if len(sys.argv) > 1 else 'llama-ablit-ctx'
c = OpenAI(api_key='sk-local', base_url='http://127.0.0.1:11435/v1', timeout=300)
msgs = [{'role': 'system', 'content': 'You implement code with tools.'},
        {'role': 'user', 'content': 'Write the file foo.py with content "x=1" '
         'using the file_write tool. Then stop.'}]
r = c.chat.completions.create(model=model, messages=msgs,
                              tools=_to_openai_tools(TOOL_DEFINITIONS),
                              tool_choice='auto', max_tokens=512)
m = r.choices[0].message
print('finish:', r.choices[0].finish_reason)
print('tool_calls:', [(t.function.name, t.function.arguments[:80]) for t in (m.tool_calls or [])])
print('content head:', (m.content or '')[:200])
