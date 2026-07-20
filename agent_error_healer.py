import asyncio
import json
import os
import re
from collections import Counter
from datetime import datetime

from dotenv import load_dotenv
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from openai import OpenAI
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessageParam,
    ChatCompletionSystemMessageParam,
    ChatCompletionToolMessageParam,
    ChatCompletionUserMessageParam,
)
from openai.types.chat import ChatCompletionToolParam

load_dotenv()

# --- Dynamic Configuration Retrieval ---
ENV_PROGRAM = os.getenv("PROGRAM_NAME", "EMPPROC")
ENV_REPO_URL = os.getenv("REPO_URL", "https://github.com/Shalini174/emp-app.git")

# Parse GitHub Owner and Repository from the parameterized URL
url_match = re.search(r"github\.com[:/]([^/]+)/([^/.]+)", ENV_REPO_URL)
REPO_OWNER = url_match.group(1) if url_match else "Shalini174"
REPO_NAME = url_match.group(2) if url_match else "emp-app"

log_file = f"{ENV_PROGRAM}_SPOOL.txt"
program_filename = f"{ENV_PROGRAM}.cbl"

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GITHUB_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

client = OpenAI(api_key=OPENAI_API_KEY)

system_prompt = f"""You are a Mainframe Systems Engineer and autonomous AI Agent specializing in automated COBOL compilation error resolution. 

Your core objective is to completely resolve the compilation errors found in the provided CI pipeline spool log. You must dynamically invoke your tools to progress from error identification to opening a successful Pull Request.

---

### OPERATIONAL WORKFLOW & TOOL LIFECYCLE
You must follow this logical progression when executing your tools. Do not skip steps.

1. Read the file from the Git Repository.
2. Identify the issue .
3. Fix the issue in the program.
4. Commit the modified program in the repository and open a pull request to merge the code to the main branch.
---

### ⚠️ CRITICAL AGENT DIRECTIVES
- **No Hallucinations:** Do not fabricate code or assume an error is fixed. You must rely purely on the real data returned by your tool executions.
- **Sequential Dependency:** You cannot invoke `code_commit` until `code_correct` has successfully run and updated the global state.
- **Observation Loop:** After every single tool execution, evaluate the text content returned by the environment. If a tool reports a failure or an API error, adjust your arguments and retry the operation.
- **Final Output:** Once `Commit_Code` returns the Pull Request details, present the final PR confirmation URL to the user as your concluding action. Do not output conversational text until the PR is opened.
"""

system_prompt1 = """You are a mainframe COBOL expert analyzing compilation spool logs.

Your task:
1. Read the spool log(provided in the user prompt) and identify all the compilation errors in it.
2. Identify the root cause of the compilation errors. 
3. Strictly send all the errors along found in the spool log, even if duplicate along with the error classification in response.
   The number of errors sent in response must match the number of errors listed at the bottom of spool log.
4. Classify the error STRICTLY into one of the following two categories:

Classification rules (MANDATORY):
- "Copybook error":
  -> Use this ONLY if the error is caused by:
     - Missing variable/data definition
     - Undefined data name
     - Incorrect structure coming from a COPYBOOK
     - Mismatch between program usage and copybook definition

- "Program error":
  -> Use this ONLY if:
     - The issue is in the COBOL program logic or syntax
     - The fix does NOT require modifying or adding definitions in a copybook

CRITICAL DECISION RULE:
If the error mentions undefined variable, missing data name ALWAYS classify it as "Copybook error".
If the error doesn't classify as Copybook error, classify it as "Program error".

Output format (STRICT JSON ONLY, NO EXTRA TEXT):
[
  {{
    "type": "<Copybook error | Program error>",
    "reason": "<clear explanation of the compilation error, sufficient to fix it>",
    "variables name": "<name of missing variables>"
  }}
]

DO NOT output anything except valid JSON.
"""

tools: list[ChatCompletionToolParam] = [
    {"type": "function", "function": {"name": "code_checkout", "description": "Reads the program files from the Github repository using MCP"}},
    {"type": "function", "function": {"name": "find_issue", "description": "Finds the issue from the compilation listing"}},
    {"type": "function", "function": {"name": "code_correct", "description": "Fixes Code based on the identified issues."}},
    {"type": "function", "function": {"name": "code_commit", "description": "Commits the changes made to the source code and opens a Pull Request."}},
]

def spool_log_read():
    if not os.path.exists(log_file):
        print(f"[WARN] Spool file {log_file} not found. Creating a blank placeholder.")
        return "No errors found. Compilation completed successfully."
    with open(log_file, "r") as f:
        return f.read()

def clean_llm_response(text: str) -> str:
    match = re.search(r"```[^\n]*\n(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text.strip()

def find_issue_logic(spool_log):
    copybook_variables = set()
    program_errors = []

    messages = [
        ChatCompletionSystemMessageParam(role="system", content=system_prompt1),
        ChatCompletionUserMessageParam(role="user", content=spool_log),
    ]
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0,
        messages=messages,
    )
    clean_message = response.choices[0].message.content.strip()

    if clean_message.startswith("```json"):
        clean_message = clean_message[7:]
    if clean_message.endswith("```"):
        clean_message = clean_message[:-3]

    try:
        resp_data = json.loads(clean_message.strip())
        for error in resp_data:
            if error.get("type") == "Copybook error":
                copybook_variables.add(error.get("variables name"))
            if error.get("type") == "Program error":
                program_errors.append(error.get("reason"))
    except Exception as e:
        print(f"Error parsing JSON analysis from LLM: {e}")

    copybook_dict = {"type_value": "Copybook error", "variables_name": list(copybook_variables)} if copybook_variables else {}
    program_dict = {"type_value": "Program error", "error_list": program_errors} if program_errors else {}
    return copybook_dict, program_dict

def fix_copybook_error(path, variable_names, reason, copybook_code):
    prompt = """
    You are a COBOL expert. Adhere strictly to the rules below while sending the response back.
    CRITICAL RULES:
    - Return only the full corrected Copybook/Copybooks
    - DO NOT return the main COBOL program
    - Add only the variable names provided in the prompt, do not add any extra variables
    - Return ONLY the full corrected copybook.No explanations or conversational text.
    - Maintain standard COBOL formatting (Margins A and B).
    - Use appropriate PIC clauses for the variables based on the context found in the spool log.
    """
    user_prompt = f"Add missing variables {variable_names} to this copybook content:\n\n{copybook_code}"
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            ChatCompletionSystemMessageParam(role="system", content=prompt),
            ChatCompletionUserMessageParam(role="user", content=user_prompt)
        ],
        temperature=0,
    )
    return response.choices[0].message.content.strip()

def fix_program_error(error_list, local_vars_list, cobol_code):
    prompt = """
    You are a COBOL expert. Adhere strictly to the rules below while sending the response back.
    CRITICAL RULES:
    - Return only the full corrected COBOL program.No explanations or conversational text.
    - Maintain standard COBOL formatting (Margins A and B).
    - ONLY fix the specific compilation errors provided in the error list.
    """
    user_prompt = f"Errors:\n{error_list}\nLocal Vars to Add:\n{local_vars_list}\nCode:\n{cobol_code}"
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            ChatCompletionSystemMessageParam(role="system", content=prompt),
            ChatCompletionUserMessageParam(role="user", content=user_prompt)
        ],
        temperature=0,
    )
    return response.choices[0].message.content.strip()

async def code_checkout(session, filename):
    file_path = f"src/{filename}"
    try:
        mcp_result = await session.call_tool(
            "get_file_contents",
            arguments={"owner": REPO_OWNER, "repo": RENAME_VAL if 'RENAME_VAL' in locals() else REPO_NAME, "path": file_path, "branch": "main"}
        )
        github_response_dict = json.loads(mcp_result.content[0].text)
        return github_response_dict.get("content", "")
    except Exception as e:
        print(f"Error checking out main file: {e}")
        return ""

async def copybook_checkout(session, file_path: str):
    try:
        mcp_result = await session.call_tool(
            "get_file_contents",
            arguments={"owner": REPO_OWNER, "repo": REPO_NAME, "path": file_path, "branch": "main"}
        )
        github_response_dict = json.loads(mcp_result.content[0].text)
        return github_response_dict.get("content", "")
    except Exception as e:
        print(f"Error checking out copybook: {e}")
        return ""

async def build_copybook_index(session, copybook_dir: str = "cpy") -> dict:
    prefix_map, suffix_map = {}, {}
    try:
        dir_result = await session.call_tool(
            "get_file_contents",
            arguments={"owner": REPO_OWNER, "repo": REPO_NAME, "path": copybook_dir, "branch": "main"}
        )
        dir_items = json.loads(dir_result.content[0].text)
        for item in dir_items:
            if item.get("type") == "file" and item.get("name", "").endswith(".cpy"):
                file_path = item.get("path")
                file_result = await session.call_tool(
                    "get_file_contents",
                    arguments={"owner": REPO_OWNER, "repo": REPO_NAME, "path": file_path, "branch": "main"}
                )
                file_data = json.loads(file_result.content[0].text)
                content = file_data.get("content", "")
                vars_found = re.findall(r"^(?:\d{6})?\s*\d{2}\s+([\w-]+)", content, re.MULTILINE)
                prefixes, suffixes = [], []
                for var in vars_found:
                    if var.upper() != "FILLER" and "-" in var:
                        parts = var.split("-")
                        prefixes.append(parts[0].upper())
                        if len(parts) > 1:
                            suffixes.append("-".join(parts[1:]).upper())
                if prefixes:
                    most_common_prefix = Counter(prefixes).most_common(1)[0][0]
                    prefix_map[most_common_prefix] = file_path
                if suffixes:
                    most_common_suffix = Counter(suffixes).most_common(1)[0][0]
                    suffix_map[most_common_suffix] = file_path
    except Exception as e:
        print(f"Error index copybooks: {e}")
    return {"prefixes": prefix_map, "suffixes": suffix_map}

def find_home_for_variable(missing_var, copybook_maps):
    if "-" not in missing_var:
        return None
    parts = missing_var.split("-")
    target_prefix = parts[0].strip().upper()
    target_suffix = "-".join(parts[1:]).strip().upper() if len(parts) > 1 else ""

    result = copybook_maps["prefixes"].get(target_prefix) or copybook_maps["suffixes"].get(target_suffix)
    if result:
        return result
    for key, path in copybook_maps["prefixes"].items():
        if key in missing_var: return path
    for key, path in copybook_maps["suffixes"].items():
        if key in missing_var: return path
    return None

async def code_correct(copy_dict, prog_dict, session, cobol_code, filename):
    copybook_mapping, program_vars_to_add, fixed_files = {}, [], {}
    copybook_map = await build_copybook_index(session=session)

    if copy_dict.get("type_value") == "Copybook error" and copy_dict.get("variables_name"):
        for var in copy_dict["variables_name"]:
            copybook_name = find_home_for_variable(var, copybook_map)
            if copybook_name:
                copybook_mapping.setdefault(copybook_name, []).append(var)
            else:
                program_vars_to_add.append(var)
        for name, sub_list in copybook_mapping.items():
            code = await copybook_checkout(session, name)
            fixed_files[name] = clean_llm_response(fix_copybook_error(name, sub_list, copy_dict, code))

    if (prog_dict.get("type_value") == "Program error" and prog_dict.get("error_list")) or program_vars_to_add:
        raw_cobol = fix_program_error(prog_dict.get("error_list", []), program_vars_to_add, cobol_code)
        fixed_files[f"src/{filename}"] = clean_llm_response(raw_cobol)
    return fixed_files

async def code_commit(session, modified_files_dict):
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    new_branch = f"feature-update-cobol-{timestamp}"
    await session.call_tool("create_branch", arguments={"owner": REPO_OWNER, "repo": REPO_NAME, "branch": new_branch, "from_branch": "main"})

    for path, content in modified_files_dict.items():
        await session.call_tool(
            "create_or_update_file", 
            arguments={"owner": REPO_OWNER, "repo": REPO_NAME, "path": path, "content": content, "message": f"Fixing {path}", "branch": new_branch}
        )
    pr_result = await session.call_tool(
        "create_pull_request",
        arguments={
            "owner": REPO_OWNER, "repo": REPO_NAME,
            "title": f"Automated Update for Mainframe Components ({timestamp})",
            "body": f"This PR updates components: {', '.join(modified_files_dict.keys())}",
            "head": new_branch, "base": "main"
        }
    )
    return pr_result.content[0].text

async def agent(log_content, session):
    copybook_errors, program_errors, cobol_code, fixed_files = {}, {}, "", {}
    messages = [ChatCompletionSystemMessageParam(role="system", content=system_prompt), ChatCompletionUserMessageParam(role="user", content=log_content)]

    while True:
        response = client.chat.completions.create(model="gpt-4o-mini", messages=messages, tools=tools)
        msg = response.choices[0].message
        assistant_msg = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            assistant_msg["tool_calls"] = msg.model_dump()["tool_calls"]
        messages.append(assistant_msg)

        if not msg.tool_calls:
            break

        for call in msg.tool_calls:
            name = call.function.name
            result = ""
            if name == "code_checkout":
                cobol_code = await code_checkout(session, program_filename)
                result = "Repository Cloned Successfully"
            elif name == "find_issue":
                copybook_errors, program_errors = find_issue_logic(log_content)
                result = "Issue detected successfully"
            elif name == "code_correct":
                fixed_files = await code_correct(copybook_errors, program_errors, session, cobol_code, program_filename)
                result = "Code corrected"
            elif name == "code_commit":
                res = await code_commit(session, fixed_files)
                result = f"Code commit done. PR Details: {res}"
            messages.append({"tool_call_id": call.id, "role": "tool", "content": str(result)})

async def main_pipeline(log_content):
    if "No errors found" in log_content:
        print("Compilation succeeded perfectly. Skipping self-healing agent step.")
        return
    env = os.environ.copy()
    env["GITHUB_PERSONAL_ACCESS_TOKEN"] = GITHUB_TOKEN
    server_params = StdioServerParameters(command="npx.cmd", args=["-y", "@modelcontextprotocol/server-github"], env=env)
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await agent(log_content, session)

if __name__ == "__main__":
    log = spool_log_read()
    asyncio.run(main_pipeline(log))