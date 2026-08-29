from ddgs.ddgs import DDGS
from typing import Literal, List, Tuple
from dataclasses import dataclass
import time
import random


@dataclass
class DDGResult:
    identifier: str
    href: str
    title: str
    body: str


def search_and_format(
        query: str,
        max_results: int,
        region: str,
        timelimit: Literal['d', 'w', 'm', 'y'],
        safesearch: Literal['on', 'moderate', 'off'],
        max_retries: int
) -> Tuple[str, dict[str, DDGResult]]:
    res = do_ddg_search(query, max_results, region,
                        timelimit, safesearch, max_retries)
    user_prompt = format_ddg_results_for_llm(res)
    res = {ele.identifier: ele for ele in res}
    return user_prompt, res


def do_ddg_search(
    query: str,
    max_results: int,
    region: str,
    timelimit: Literal['d', 'w', 'm', 'y'],
    safesearch: Literal['on', 'moderate', 'off'],
    max_retries: int
) -> list[DDGResult]:
    # Initialize the DuckDuckGo search object
    results = list()
    with DDGS() as ddgs:
        for i in range(1, max_retries + 1):
            try:
                # Perform a text search and limit to 5 results
                results = ddgs.text(
                    query,
                    max_results=max_results,
                    region=region,
                    safesearch=safesearch,
                    timelimit=timelimit,
                )
            except Exception as ex:
                print(ex)
                sleep_time = min(2 ** i, 60)
                sleep_time = random.uniform(sleep_time / 2, sleep_time)
                sleep_time = max(sleep_time, 2)
                print(f"Retrying DDG in {int(sleep_time)} seconds ({i + 1}/{max_retries}).")
                time.sleep(sleep_time)
    if len(results) == 0:
        raise ValueError(f"Failed to retrieve results for query: {query}")
    parsed_results = [
        DDGResult(
            identifier=_get_letter_identifier(i),
            **res
        )
        for i, res in enumerate(results)
    ]
    return parsed_results


def format_ddg_results_for_llm(results: List[DDGResult]) -> str:
    """Formats a list of DDGResult objects into a string prompt for the LLM."""
    if not results:
        return "No search results provided."

    prompt_lines = [
        "Please evaluate the following search results based on the system instructions:\n"
    ]

    for i, result in enumerate(results):
        # Structure each result cleanly so the LLM can easily parse the fields
        item_text = (
            f"[{result.identifier}]\n"
            f"Title: {result.title}\n"
            f"URL: {result.href}\n"
            f"Body: {result.body}\n"
        )
        prompt_lines.append(item_text)

    return "\n".join(prompt_lines)


def _get_letter_identifier(index: int) -> str:
    """Converts a 0-based index to an alphabetical identifier (A, B, ..., Z, AA, ...)."""
    chars = []
    while True:
        chars.append(chr(65 + (index % 26)))
        index = index // 26 - 1
        if index < 0:
            break
    return "".join(reversed(chars))
