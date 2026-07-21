import os
import sys  # <--- Added import
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

async def run_mcp_pipeline_poc(session, original_code: str, modified_code: str):
    clean_name = program_name.replace('.cbl', '').upper().strip()
    final_code = modified_code
    jcl_content, jcl_path = None, None
    
    # 1. Attempt to find matching JCL safely
    try:
        dir_result = await session.call_tool(
            "get_file_contents", 
            arguments={"owner": REPO_OWNER, "repo": REPO_NAME, "path": "jcl"}
        )
        dir_data = json.loads(dir_result.content[0].text)
        
        if isinstance(dir_data, list):
            for file_entry in dir_data:
                file_name = file_entry.get("name", "")
                if not file_name.upper().endswith(".JCL"):
                    continue
                
                path = f"jcl/{file_name}"
                file_result = await session.call_tool(
                    "get_file_contents", 
                    arguments={"owner": REPO_OWNER, "repo": REPO_NAME, "path": path}
                )
                raw = file_result.content[0].text
                try:
                    parsed = json.loads(raw)
                    content = base64.b64decode(parsed.get("content", "").replace("\n", "")).decode("utf-8")
                except Exception:
                    content = raw
                    
                if f"EXEC PGM={clean_name}" in content.upper():
                    jcl_content, jcl_path = content, path
                    break
    except Exception as e:
        print(f"[WARN] JCL directory search skipped ({e}). Proceeding without JCL healing.")

    # 2. Perform FD healing if JCL is found
    if jcl_content:
        print(f"[INFO] Found matching JCL at '{jcl_path}'. Performing FD section healing...")
        jcl_datasets = await extract_jcl_dd_allocations(jcl_content, program_name)
        file_specs = [{"dd_name": ds.get("dd_name"), "actual_lrecl": ds.get("lrecl") or 80} for ds in jcl_datasets]
        final_code = await heal_cobol_fd_section(modified_code, file_specs)
    else:
        print(f"[INFO] No matching JCL found for PGM={clean_name}. Proceeding with static analysis fixes.")

    # 3. Check code diff & set exit codes
    if final_code.strip() != original_code.strip():
        print("[INFO] Code changes detected! Creating branch and Pull Request...")
        pr_url = await code_commit(session, final_code)
        print(f"[ACTION REQUIRED] Pull Request opened: {pr_url}")
        return 10  # Return Code 10 -> PR Created
    else:
        print("[INFO] Code adheres to rules. No changes required.")
        return 0   # Return Code 0 -> Success / No PR Needed

async def static_analysis_check(session) -> tuple[str, str]:
    file_path = f"src/{program_name}"
    cleaned_path = "/".join(part.strip() for part in file_path.split("/"))
    mcp_result = await session.call_tool(
        "get_file_contents", 
        arguments={"owner": REPO_OWNER, "repo": REPO_NAME, "path": cleaned_path, "branch": "main"}
    )
    raw_content = json.loads(mcp_result.content[0].text).get("content", "")
    cobol_code = base64.b64decode(raw_content.replace("\n", "")).decode("utf-8") if "DIVISION" not in raw_content.upper() else raw_content
    
    with open(rules, "r") as f: 
        z = f.read()

    static_analysis_prompt = """You are an expert IBM Mainframe COBOL static analysis specialist.
Analyze the provided COBOL source code against the static analysis rules.
Do NOT regenerate the entire COBOL file. Instead, output ONLY Search & Replace blocks for lines that violate rules.

Format every edit strictly as:
<<<<<<< SEARCH
[Exact original lines from source code to replace]
=======
[Corrected lines adhering strictly to 8-72 column alignment]
>>>>>>>

Rules to strictly follow:
1. Preserve exact line order. Fix violations IN-PLACE right where they occur in the original code.
2. Apply the smallest possible change. Do not move statements to higher paragraphs.
3. Keep standard COBOL fixed format (columns 8-72).
4. Output ONLY the search/replace blocks. No explanations, no markdown wrappers outside blocks.
"""

    response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=8096,
    temperature=0.0,
    system=[
        {
            "type": "text",
            "text": static_analysis_prompt
        },
        {
            "type": "text",
            "text": f"STATIC ANALYSIS RULES:\n{z}",
            "cache_control": {"type": "ephemeral"}  # Caches rules across pipeline runs
        }
    ],
    messages=[MessageParam(role="user", content=f"COBOL Source:\n{cobol_code}")]
)
    
    patch_text = response.content[0].text.strip()
    modified_code = apply_search_replace_patches(cobol_code, patch_text)
    return cobol_code, modified_code


def apply_search_replace_patches(original_code: str, patch_text: str) -> str:
    """Applies <<<<<<< SEARCH ... ======= ... >>>>>>> blocks sequentially."""
    pattern = re.compile(r"<<<<<<< SEARCH\n(.*?)\n=======\n(.*?)\n>>>>>>>", re.DOTALL)
    matches = pattern.findall(patch_text)
    
    updated_code = original_code
    for search_block, replace_block in matches:
        if search_block in updated_code:
            updated_code = updated_code.replace(search_block, replace_block, 1)
        else:
            print(f"[WARN] Could not match search block in source:\n{search_block[:50]}...")
            
    return updated_code

async def github_connection():
    env = os.environ.copy()
    env["GITHUB_PERSONAL_ACCESS_TOKEN"] = GITHUB_TOKEN
    server_params = StdioServerParameters(command="npx.cmd", args=["-y", "@modelcontextprotocol/server-github"], env=env)
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            original_code, modified_code = await static_analysis_check(session)
            return await run_mcp_pipeline_poc(session, original_code, modified_code)

async def code_commit(session, modified_file) -> str:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    new_branch = f"feature-static-analysis-{timestamp}"
    await session.call_tool("create_branch", arguments={"owner": REPO_OWNER, "repo": REPO_NAME, "branch": new_branch, "from_branch": "main"})
    await session.call_tool(
        "create_or_update_file",
        arguments={"owner": REPO_OWNER, "repo": REPO_NAME, "path": f"src/{program_name}", "content": modified_file, "message": "Applying static patches", "branch": new_branch}
    )
    pr_res = await session.call_tool(
        "create_pull_request",
        arguments={"owner": REPO_OWNER, "repo": REPO_NAME, "title": f"Static Analysis Fixes ({timestamp})", "body": "Applying strict structural formatting rules", "head": new_branch, "base": "main"}
    )
    
    pr_url = f"https://github.com/{REPO_OWNER}/{REPO_NAME}/pulls"
    try:
        parsed = json.loads(pr_res.content[0].text)
        pr_url = parsed.get("html_url") or parsed.get("url") or pr_url
    except Exception:
        pass
        
    return pr_url

if __name__ == "__main__":
    exit_code = asyncio.run(github_connection())
    sys.exit(exit_code)
