import json
from typing import Optional

from event_crawler.llm.lazy_ollama import LazyOllamaChat, Chatlike
from event_crawler.llm.interfaces import OllamaModelOptions


class LazyJsonOllamaException(Exception):
    pass


class LazyJsonOllama(Chatlike):
    def __init__(self, chat: LazyOllamaChat, max_retries: int = 2):
        self._chat = chat
        self._max_retries = max_retries

    def chat(
        self,
        user_message: str,
        system_message: str | None = None,
        options: Optional[OllamaModelOptions] = None,
        *,
        output_model: dict | list | None = None
    ) -> dict:
        if output_model is None:
            output_model = dict()
        if isinstance(output_model, list):
            output_model = {key: None for key in output_model}

        excs = list()
        for i in range(self._max_retries):
            try:
                result = self.__json_chat(user_message, system_message, options, output_model)
                return result
            except LazyJsonOllamaException as ex:
                print(
                    f"Failed producing valid JSON ({i + 1}/{self._max_retries}). Retrying...")
                excs.append(ex)

        raise ExceptionGroup(f"Failed to produce valid JSON after {self._max_retries} attempts.", excs)

    def __json_chat(self, user_message: str, system_message: str | None, options: OllamaModelOptions | None, output_model: dict) -> dict:
        response = self._chat.chat(user_message, system_message, options)
        try:
            data = json.loads(response)
        except json.JSONDecodeError as ex:
            data = _salvage_truncated_json(response)
            if data is None:
                raise LazyJsonOllamaException() from ex
        data = _validate_and_fill_json(data, output_model)
        return data


def _validate_and_fill_json(data: dict, output_model: dict) -> dict:
    """
    Validates that the keys in the data match the expected output model.
    If a key is missing in the data, it will be filled with the default value from the output model.
    If an unexpected key is found in the data, an exception will be raised.
    """
    for key in data.keys():
        if key not in output_model:
            raise LazyJsonOllamaException(
                f"Unexpected key '{key}' found in data.")
    for key in output_model.keys():
        if key not in data:
            data[key] = output_model[key]
    return data


def _salvage_truncated_json(broken_json_str: str) -> list[dict]:
    """
    Attempts to salvage fully completed JSON objects from a truncated JSON array string.
    Discards the partially generated object at the end.
    """
    broken_json_str = broken_json_str.strip()

    # If it doesn't even start with an array bracket, we can't parse it
    if not broken_json_str.startswith('['):
        return None

    # Step 1: Try to parse it normally in case it isn't actually broken
    try:
        return json.loads(broken_json_str)
    except json.JSONDecodeError:
        pass  # It is truncated, proceed to salvage operations

    # Step 2: Find the last closing brace of a completed object
    last_brace_index = broken_json_str.rfind('}')

    if last_brace_index == -1:
        # The LLM didn't even manage to finish a single event
        return None

    # Step 3: Slice the string to keep everything up to the last complete object,
    # and properly close the JSON array.
    salvaged_str = broken_json_str[:last_brace_index + 1] + ']'

    # Step 4: Verify the salvaged string is now valid JSON
    try:
        return json.loads(salvaged_str)
    except json.JSONDecodeError:
        # If it still fails, the internal structure is deeply corrupted
        return None
