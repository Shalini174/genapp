import os
import json
import asyncio
import base64
import re
from datetime import datetime
from dotenv import load_dotenv
from anthropic import Anthropic
from anthropic.types import MessageParam
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.session import ClientSession

load_dotenv()

# --- Dynamic Configuration Retrieval ---
ENV_PROGRAM = os.getenv("PROGRAM_NAME", "EMPPROC")
ENV_REPO_URL = os.getenv("REPO_URL", "https://github.com/Shalini174/emp-app.git")

url_match = re.search(r"github\.com[:/]([^/]+)/([^/.]+)", ENV_REPO_URL)
REPO_OWNER = url_match.group(1) if url_match else "Shalini174"
REPO_NAME = url_match.group(2) if url_match else "emp-app"

rules = 'COBOL_Static_Analysis_Rules.txt'
program_name = f"{ENV_PROGRAM}.cbl"

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

async def extract_jcl_dd_allocations(jcl_content: str, prog_name: str) -> list:
    system_prompt = f"""
    You are a mainframe JCL expert. Analyze the provided JCL.
    Find the EXEC step that runs PGM={prog_name.replace('.cbl','')}.
    Extract all dataset allocations (DD statements).
    Return a JSON array with keys: dd_name, dsn, disp, lrecl.
    Respond ONLY with valid JSON.
    """
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=system_prompt,
        messages=[MessageParam(role="user", content=jcl_content)]
    )
    cleaned = response.content[0].text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"): cleaned = cleaned[4:]
    try:
        res = json.loads(cleaned.strip())
        return res if isinstance(res, list) else res.get("datasets", [])
    except Exception:
        return []

async def heal_cobol_fd_section(cobol_content: str, file_specs: list) -> str:
    system_prompt = """
    You are an autonomous COBOL DevOps agent. 
    Task: Match FD names to DD names. If FD lengths do not match 'actual_lrecl', 
    intelligently modify the COBOL FD section (RECORD CONTAINS and 01 levels) to match 'actual_lrecl'.
    Return the full, corrected COBOL code. Ensure exact COBOL column formatting (columns 8-72).
    Respond ONLY with the raw COBOL code. No markdown, no explanations.
    """
    user_content = f"Ground-Truth Specs:\n{json.dumps(file_specs)}\n\nCOBOL Code:\n{cobol_content}"
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8096,
        system=system_prompt,
        messages=[MessageParam(role="user", content=user_content)]
    )
    return response.content[0].text.replace("```cobol", "").replace("```", "").strip()

async def run_mcp_pipeline_poc(session, modified_code: str):
    clean_name = program_name.replace('.cbl', '').upper().strip()
    
    dir_result = await session.call_tool("get_file_contents", arguments={"owner": REPO_OWNER, "repo": REPO_NAME, "path": "jcl"})
    dir_data = json.loads(dir_result.content[0].text)
    
    jcl_content, jcl_path = None, None
    for file_entry in dir_data:
        file_name = file_entry.get("name", "")
        if not (file_name.upper().endswith(".JCL")): continue
        
        path = f"jcl/{file_name}"
        file_result = await session.call_tool("get_file_contents", arguments={"owner": REPO_OWNER, "repo": REPO_NAME, "path": path})
        raw = file_result.content[0].text
        try:
            parsed = json.loads(raw)
            content = base64.b64decode(parsed.get("content", "").replace("\n", "")).decode("utf-8")
        except Exception:
            content = raw
            
        if f"EXEC PGM={clean_name}" in content.upper():
            jcl_content, jcl_path = content, path
            break

    if not jcl_content:
        print(f"[WARN] No related configuration JCL discovered running PGM={clean_name}")
        return

    jcl_datasets = await extract_jcl_dd_allocations(jcl_content, program_name)
    file_specs = [{"dd_name": ds.get("dd_name"), "actual_lrecl": ds.get("lrecl") or 80} for ds in jcl_datasets]
    
    healed_cobol = await heal_cobol_fd_section(modified_code, file_specs)
    await code_commit(session, healed_cobol)

async def static_analysis_check(session) -> str:
    file_path = f"src/{program_name}"
    cleaned_path = "/".join(part.strip() for part in file_path.split("/"))
    print(f"DEBUG - Cleaned file_path: {repr(cleaned_path)}")
    mcp_result = await session.call_tool("get_file_contents", arguments={"owner": REPO_OWNER, "repo": REPO_NAME, "path": cleaned_path, "branch": "main"})
    raw_content = json.loads(mcp_result.content[0].text).get("content", "")
    
    cobol_code = base64.b64decode(raw_content.replace("\n", "")).decode("utf-8") if "DIVISION" not in raw_content.upper() else raw_content
    
    if not os.path.exists(rules):
        with open(rules, "w") as f: f.write("Rule 1: Adhere to structural fixed alignment margins.")
        
    with open(rules, "r") as f: z = f.read()

    static_analysis_prompt = """You are an expert IBM Mainframe COBOL static analysis specialist.
    Return ONLY the raw modified COBOL source. No markdown, no code blocks or conversational text.
    """
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8096,
        system=static_analysis_prompt,
        messages=[MessageParam(role="user", content=f"COBOL:\n{cobol_code}\n\nRules:\n{z}")]
    )
    return response.content[0].text.strip()

async def github_connection():
    env = os.environ.copy()
    env["GITHUB_PERSONAL_ACCESS_TOKEN"] = GITHUB_TOKEN
    server_params = StdioServerParameters(command="npx.cmd", args=["-y", "@modelcontextprotocol/server-github"], env=env)
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            modified_code = await static_analysis_check(session)
            await run_mcp_pipeline_poc(session, modified_code)

async def code_commit(session, modified_file):
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    new_branch = f"feature-static-analysis-{timestamp}"
    await session.call_tool("create_branch", arguments={"owner": REPO_OWNER, "repo": REPO_NAME, "branch": new_branch, "from_branch": "main"})
    await session.call_tool(
        "create_or_update_file",
        arguments={"owner": REPO_OWNER, "repo": REPO_NAME, "path": f"src/{program_name}", "content": modified_file, "message": "Applying static patches", "branch": new_branch}
    )
    await session.call_tool(
        "create_pull_request",
        arguments={"owner": REPO_OWNER, "repo": REPO_NAME, "title": f"Static Analysis Fixes ({timestamp})", "body": "Applying strict structural formatting rules", "head": new_branch, "base": "main"}
    )

if __name__ == "__main__":
    asyncio.run(github_connection())
