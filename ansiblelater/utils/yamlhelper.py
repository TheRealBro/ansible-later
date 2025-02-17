"""Utils for YAML file operations."""

# Copyright (c) 2013-2014 Will Thames <will@thames.id.au>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

import codecs
import os
from contextlib import suppress

import ansible.parsing.mod_args
import yaml
from ansible import constants
from ansible.errors import AnsibleError, AnsibleParserError
from ansible.parsing.dataloader import DataLoader
from ansible.parsing.mod_args import ModuleArgsParser
from ansible.parsing.yaml.constructor import AnsibleConstructor
from ansible.parsing.yaml.loader import AnsibleLoader
from ansible.template import Templar
from yaml.composer import Composer

from ansiblelater.exceptions import LaterAnsibleError, LaterError

try:
    # Try to import the Ansible 2 module first, it's the future-proof one
    from ansible.parsing.splitter import split_args

except ImportError:
    # Fallback on the Ansible 1.9 module
    from ansible.module_utils.splitter import split_args


def parse_yaml_from_file(filepath):
    dl = DataLoader()
    return dl.load_from_file(filepath)


def path_dwim(basedir, given):
    dl = DataLoader()
    dl.set_basedir(basedir)
    return dl.path_dwim(given)


def ansible_template(basedir, varname, templatevars, **kwargs):
    dl = DataLoader()
    dl.set_basedir(basedir)
    templar = Templar(dl, variables=templatevars)
    return templar.template(varname, **kwargs)


try:
    from ansible.plugins import module_loader
except ImportError:
    from ansible.plugins.loader import init_plugin_loader, module_loader

    init_plugin_loader()

LINE_NUMBER_KEY = "__line__"
FILENAME_KEY = "__file__"

VALID_KEYS = [
    "name",
    "action",
    "when",
    "async",
    "poll",
    "notify",
    "first_available_file",
    "include",
    "import_playbook",
    "tags",
    "register",
    "ignore_errors",
    "delegate_to",
    "local_action",
    "transport",
    "remote_user",
    "sudo",
    "sudo_user",
    "sudo_pass",
    "when",
    "connection",
    "environment",
    "args",
    "always_run",
    "any_errors_fatal",
    "changed_when",
    "failed_when",
    "check_mode",
    "delay",
    "retries",
    "until",
    "su",
    "su_user",
    "su_pass",
    "no_log",
    "run_once",
    "become",
    "become_user",
    "become_method",
    FILENAME_KEY,
]

BLOCK_NAME_TO_ACTION_TYPE_MAP = {
    "tasks": "task",
    "handlers": "handler",
    "pre_tasks": "task",
    "post_tasks": "task",
    "block": "meta",
    "rescue": "meta",
    "always": "meta",
}


def tokenize(line):
    tokens = line.lstrip().split(" ")
    if tokens[0] == "-":
        tokens = tokens[1:]
    if tokens[0] == "action:" or tokens[0] == "local_action:":
        tokens = tokens[1:]
    command = tokens[0].replace(":", "")

    args = []
    kwargs = {}
    nonkvfound = False
    for arg in tokens[1:]:
        if "=" in arg and not nonkvfound:
            kv = arg.split("=", 1)
            kwargs[kv[0]] = kv[1]
        else:
            nonkvfound = True
            args.append(arg)
    return (command, args, kwargs)


def _playbook_items(pb_data):
    if isinstance(pb_data, dict):
        return pb_data.items()

    if not pb_data:
        return []

    return [item for play in pb_data for item in play.items()]


def find_children(playbook, playbook_dir):
    if not os.path.exists(playbook[0]):
        return []
    if playbook[1] == "role":
        playbook_ds = {"roles": [{"role": playbook[0]}]}
    else:
        try:
            playbook_ds = parse_yaml_from_file(playbook[0])
        except AnsibleError as e:
            raise SystemExit(str(e)) from e
    results = []
    basedir = os.path.dirname(playbook[0])
    items = _playbook_items(playbook_ds)
    for item in items:
        for child in play_children(basedir, item, playbook[1], playbook_dir):
            if "$" in child["path"] or "{{" in child["path"]:
                continue
            valid_tokens = []
            for token in split_args(child["path"]):
                if "=" in token:
                    break
                valid_tokens.append(token)
            path = " ".join(valid_tokens)
            results.append({"path": path_dwim(basedir, path), "type": child["type"]})
    return results


def template(basedir, value, variables, fail_on_undefined=False, **kwargs):
    # Hack to skip the following exception when using to_json filter on a variable.
    # I guess the filter doesn't like empty vars...
    with suppress(AnsibleError, ValueError):
        return ansible_template(
            os.path.abspath(basedir),
            value,
            variables,
            **dict(kwargs, fail_on_undefined=fail_on_undefined),
        )


def play_children(basedir, item, parent_type):
    delegate_map = {
        "tasks": _taskshandlers_children,
        "pre_tasks": _taskshandlers_children,
        "post_tasks": _taskshandlers_children,
        "block": _taskshandlers_children,
        "include": _include_children,
        "import_playbook": _include_children,
        "roles": _roles_children,
        "dependencies": _roles_children,
        "handlers": _taskshandlers_children,
    }
    (k, v) = item
    play_library = os.path.join(os.path.abspath(basedir), "library")
    _load_library_if_exists(play_library)

    if k in delegate_map and v:
        v = template(
            os.path.abspath(basedir),
            v,
            {"playbook_dir": os.path.abspath(basedir)},
            fail_on_undefined=False,
        )
        return delegate_map[k](basedir, k, v, parent_type)
    return []


def _include_children(basedir, k, v, parent_type):
    # handle include: filename.yml tags=blah
    (command, args, kwargs) = tokenize(f"{k}: {v}")

    result = path_dwim(basedir, args[0])
    if not os.path.exists(result) and not basedir.endswith("tasks"):
        result = path_dwim(os.path.join(basedir, "..", "tasks"), v)
    return [{"path": result, "type": parent_type}]


def _taskshandlers_children(basedir, k, v, parent_type):
    results = []
    for th in v:
        if "include" in th:
            append_children(th["include"], basedir, k, parent_type, results)
        elif "include_tasks" in th:
            append_children(th["include_tasks"], basedir, k, parent_type, results)
        elif "import_playbook" in th:
            append_children(th["import_playbook"], basedir, k, parent_type, results)
        elif "import_tasks" in th:
            append_children(th["import_tasks"], basedir, k, parent_type, results)
        elif "import_role" in th:
            results.extend(
                _roles_children(
                    basedir,
                    k,
                    [th["import_role"].get("name")],
                    parent_type,
                    main=th["import_role"].get("tasks_from", "main"),
                )
            )
        elif "include_role" in th:
            results.extend(
                _roles_children(
                    basedir,
                    k,
                    [th["include_role"].get("name")],
                    parent_type,
                    main=th["include_role"].get("tasks_from", "main"),
                )
            )
        elif "block" in th:
            results.extend(_taskshandlers_children(basedir, k, th["block"], parent_type))
            if "rescue" in th:
                results.extend(_taskshandlers_children(basedir, k, th["rescue"], parent_type))
            if "always" in th:
                results.extend(_taskshandlers_children(basedir, k, th["always"], parent_type))
    return results


def append_children(taskhandler, basedir, k, parent_type, results):
    # when taskshandlers_children is called for playbooks, the
    # actual type of the included tasks is the section containing the
    # include, e.g. tasks, pre_tasks, or handlers.
    playbook_section = k if parent_type == "playbook" else parent_type
    results.append({"path": path_dwim(basedir, taskhandler), "type": playbook_section})


def _roles_children(basedir, k, v, parent_type, main="main"):  # noqa
    results = []
    for role in v:
        if isinstance(role, dict):
            if "role" in role or "name" in role:
                if "tags" not in role or "skip_ansible_later" not in role["tags"]:
                    results.extend(
                        _look_for_role_files(
                            basedir, role.get("role", role.get("name")), main=main
                        )
                    )
            else:
                raise SystemExit(f"role dict {role} does not contain a 'role' or 'name' key")
        else:
            results.extend(_look_for_role_files(basedir, role, main=main))
    return results


def _load_library_if_exists(path):
    if os.path.exists(path):
        module_loader.add_directory(path)


def _rolepath(basedir, role):
    role_path = None

    possible_paths = [
        # if included from a playbook
        path_dwim(basedir, os.path.join("roles", role)),
        path_dwim(basedir, role),
        # if included from roles/[role]/meta/main.yml
        path_dwim(basedir, os.path.join("..", "..", "..", "roles", role)),
        path_dwim(basedir, os.path.join("..", "..", role)),
    ]

    if constants.DEFAULT_ROLES_PATH:
        search_locations = constants.DEFAULT_ROLES_PATH
        if isinstance(search_locations, str):
            search_locations = search_locations.split(os.pathsep)
        for loc in search_locations:
            loc = os.path.expanduser(loc)
            possible_paths.append(path_dwim(loc, role))

    for path_option in possible_paths:
        if os.path.isdir(path_option):
            role_path = path_option
            break

    if role_path:
        _load_library_if_exists(os.path.join(role_path, "library"))

    return role_path


def _look_for_role_files(basedir, role, main="main"):
    role_path = _rolepath(basedir, role)
    if not role_path:
        return []

    results = []

    for th in ["tasks", "handlers", "meta"]:
        for ext in (".yml", ".yaml"):
            thpath = os.path.join(role_path, th, main + ext)
            if os.path.exists(thpath):
                results.append({"path": thpath, "type": th})
                break
    return results


def rolename(filepath):
    idx = filepath.find("roles/")
    if idx < 0:
        return ""
    role = filepath[idx + 6 :]
    return role[: role.find("/")]


def _kv_to_dict(v):
    (command, args, kwargs) = tokenize(v)
    return dict(__ansible_module__=command, __ansible_arguments__=args, **kwargs)


def normalize_task(task, filename, custom_modules=None):
    """Ensure tasks have an action key and strings are converted to python objects."""

    if custom_modules is None:
        custom_modules = []

    ansible_action_type = task.get("__ansible_action_type__", "task")
    if "__ansible_action_type__" in task:
        del task["__ansible_action_type__"]

    # temp. extract metadata
    ansible_meta = {}
    for key in ["__line__", "__file__", "__ansible_action_meta__"]:
        default = None

        if key == "__ansible_action_meta__":
            default = {}

        ansible_meta[key] = task.pop(key, default)

    normalized = {}

    builtin = list(ansible.parsing.mod_args.BUILTIN_TASKS)
    builtin = list(set(builtin + custom_modules))
    ansible.parsing.mod_args.BUILTIN_TASKS = frozenset(builtin)
    mod_arg_parser = ModuleArgsParser(task)

    try:
        action, arguments, normalized["delegate_to"] = mod_arg_parser.parse()
    except AnsibleParserError as e:
        raise LaterAnsibleError(e) from e

    # denormalize shell -> command conversion
    if "_uses_shell" in arguments:
        action = "shell"
        del arguments["_uses_shell"]

    for k, v in list(task.items()):
        if k in ("action", "local_action", "args", "delegate_to") or k == action:
            # we don"t want to re-assign these values, which were
            # determined by the ModuleArgsParser() above
            continue

        normalized[k] = v

    normalized["action"] = {"__ansible_module__": action}

    if "_raw_params" in arguments:
        normalized["action"]["__ansible_arguments__"] = arguments["_raw_params"].strip().split()
        del arguments["_raw_params"]
    else:
        normalized["action"]["__ansible_arguments__"] = []
    normalized["action"].update(arguments)

    normalized[FILENAME_KEY] = filename
    normalized["__ansible_action_type__"] = ansible_action_type

    # add back extracted metadata
    for k, v in ansible_meta.items():
        if v:
            normalized[k] = v

    return normalized


def action_tasks(yaml, file):
    tasks = []
    if file["filetype"] in ["tasks", "handlers"]:
        tasks = add_action_type(yaml, file["filetype"])
    else:
        tasks.extend(extract_from_list(yaml, ["tasks", "handlers", "pre_tasks", "post_tasks"]))

    # Add sub-elements of block/rescue/always to tasks list
    tasks.extend(extract_from_list(tasks, ["block", "rescue", "always"]))
    # Remove block/rescue/always elements from tasks list
    block_rescue_always = ("block", "rescue", "always")
    tasks[:] = [task for task in tasks if all(k not in task for k in block_rescue_always)]

    allowed = ["include", "include_tasks", "import_playbook", "import_tasks"]

    return [task for task in tasks if set(allowed).isdisjoint(task.keys())]


def task_to_str(task):
    name = task.get("name")
    if name:
        return name
    action = task.get("action")
    args = " ".join(
        [
            f"{k}={v}"
            for (k, v) in action.items()
            if k not in ["__ansible_module__", "__ansible_arguments__"]
        ]
        + action.get("__ansible_arguments__")
    )
    return "{} {}".format(action["__ansible_module__"], args)


def extract_from_list(blocks, candidates):
    results = []
    for block in blocks:
        for candidate in candidates:
            delete_meta_keys = [candidate, "__line__", "__file__", "__ansible_action_type__"]
            if isinstance(block, dict) and candidate in block:
                if isinstance(block[candidate], list):
                    meta_data = dict(block)
                    for key in delete_meta_keys:
                        meta_data.pop(key, None)
                    results.extend(add_action_type(block[candidate], candidate, meta_data))
                elif block[candidate] is not None:
                    raise RuntimeError(
                        f"Key '{candidate}' defined, but bad value: '{block[candidate]!s}'"
                    )
    return results


def add_action_type(actions, action_type, action_meta=None):
    results = []
    for action in actions:
        action["__ansible_action_type__"] = BLOCK_NAME_TO_ACTION_TYPE_MAP[action_type]
        if action_meta:
            action["__ansible_action_meta__"] = action_meta
        results.append(action)
    return results


def parse_yaml_linenumbers(data, filename):
    """
    Parse yaml as ansible.utils.parse_yaml but with linenumbers.

    The line numbers are stored in each node's LINE_NUMBER_KEY key.

    """

    def compose_node(parent, index):
        # the line number where the previous token has ended (plus empty lines)
        line = loader.line
        node = Composer.compose_node(loader, parent, index)
        node.__line__ = line + 1
        return node

    def construct_mapping(node, deep=False):
        try:
            mapping = AnsibleConstructor.construct_mapping(loader, node, deep=deep)
        except yaml.constructor.ConstructorError as e:
            raise LaterError("syntax error", e) from e

        if hasattr(node, "__line__"):
            mapping[LINE_NUMBER_KEY] = node.__line__
        else:
            mapping[LINE_NUMBER_KEY] = mapping._line_number
        mapping[FILENAME_KEY] = filename
        return mapping

    try:
        kwargs = {}
        loader = AnsibleLoader(data, **kwargs)
        loader.compose_node = compose_node
        loader.construct_mapping = construct_mapping
        data = loader.get_single_data() or []
    except (
        yaml.parser.ParserError,
        yaml.scanner.ScannerError,
        yaml.constructor.ConstructorError,
    ) as e:
        raise LaterError("syntax error", e) from e
    except yaml.composer.ComposerError as e:
        e.problem = f"{e.context} {e.problem}"
        raise LaterError("syntax error", e) from e
    return data


def normalized_yaml(file, options):
    lines = []
    removes = []

    try:
        with codecs.open(file, mode="rb", encoding="utf-8") as f:
            lines = list(enumerate(f.readlines(), start=1))

        for i, line in lines:
            if line.strip().startswith("#"):
                removes.append((i, line))
            # remove document starter also
            if options.get("remove_markers") and line.strip() == "---":
                removes.append((i, line))
            # remove empty lines
            if options.get("remove_empty") and not line.strip():
                removes.append((i, line))

        for line in removes:
            lines.remove(line)
    except (yaml.parser.ParserError, yaml.scanner.ScannerError) as e:
        raise LaterError("syntax error", e) from e
    return lines


class UnsafeTag:
    """Handle custom yaml unsafe tag."""

    yaml_tag = "!unsafe"

    def __init__(self, value):
        self.unsafe = value

    @staticmethod
    def yaml_constructor(loader, node):
        return loader.construct_scalar(node)


class VaultTag:
    """Handle custom yaml vault tag."""

    yaml_tag = "!vault"

    def __init__(self, value):
        self.unsafe = value

    @staticmethod
    def yaml_constructor(loader, node):
        return loader.construct_scalar(node)
