import re
import ast
import itertools
import functools
import logging

try:
    fast_string = str
    str = unicode
except NameError:
    long = int
    basestring = str
    fast_string = str

try:
    from collections import OrderedDict
except ImportError:
    from ordereddict import OrderedDict

try:
    import __builtin__ as builtins
except ImportError:
    import builtins

from .astutil import load
from .astutil import store
from .astutil import param
from .astutil import swap
from .astutil import subscript
from .astutil import NameLookupRewriteTransformer
from .astutil import Comment
from .astutil import Static
from .astutil import Symbol
from .astutil import Builtin
from .astutil import Node

from .codegen import TemplateCodeGenerator
from .codegen import template

from .tales import StringExpr
from .i18n import fast_translate

from .nodes import Text
from .nodes import Expression

from .config import DEBUG_MODE
from .exc import TranslationError
from .utils import DebuggingOutputStream
from .utils import Placeholder


log = logging.getLogger('chameleon.compiler')

COMPILER_INTERNALS_OR_DISALLOWED = set([
    "stream",
    "append",
    "econtext",
    "rcontext",
    "translate",
    "decode",
    "convert",
    "str",
    "len",
    ])


RE_AMP = Static(
    template(
        "re.compile(r'&(?!([A-Za-z]+|#[0-9]+);)')",
        re=Symbol(re),
        mode="eval")
        )

RE_REDUCE_WS = Static(
    template(
        "functools.partial(re.compile(r'\s+').sub, ' ')",
        re=Symbol(re),
        functools=Symbol(functools),
        mode="eval")
    )

RE_MANGLE = re.compile('[\-: ]')

if DEBUG_MODE:
    LIST = template("cls()", cls=DebuggingOutputStream, mode="eval")
else:
    LIST = template("[]", mode="eval")


def identifier(prefix, suffix=None):
    return "_%s_%s" % (prefix, mangle(suffix or id(prefix)))


def mangle(string):
    return RE_MANGLE.sub('_', str(string)).replace('\n', '')


def load_econtext(name):
    return subscript(name, load("econtext"), ast.Load())


def store_econtext(name):
    name = fast_string(name)
    return subscript(name, load("econtext"), ast.Store())


def store_rcontext(name):
    name = fast_string(name)
    return subscript(name, load("rcontext"), ast.Store())


@template
def emit_node(node, append="append"):  # pragma: no cover
    append(node)


@template
def emit_convert(target, native=bytes, str=str, long=long):  # pragma: no cover
    if target is not None:
        _tt = type(target)

        if _tt is int or _tt is float or _tt is long:
            target = native(target)
        elif _tt is native:
            target = decode(target)
        elif _tt is not str:
            try:
                target = target.__html__
            except AttributeError:
                target = convert(target)
            else:
                target = target()


@template
def emit_translate(target, msgid, default=None):  # pragma: no cover
    target = translate(msgid, default=default, domain=_i18n_domain)


@template
def emit_convert_and_escape(
    target, msgid, quote=ast.Str("\0"), str=str, long=long,
    native=bytes, re_amp=RE_AMP):  # pragma: no cover
    if target is None:
        pass
    elif target is False:
        target = None
    else:
        _tt = type(target)

        if _tt is int or _tt is float or _tt is long:
            target = str(target)
        else:
            try:
                if _tt is native:
                    target = decode(msgid)
                elif _tt is not str:
                    try:
                        target = target.__html__
                    except:
                        target = convert(msgid)
                    else:
                        raise RuntimeError
            except RuntimeError:
                target = target()
            else:
                # character escape
                if '&' in target:
                    if ';' in target:
                        target = re_amp.sub('&amp', target)
                    else:
                        target = target.replace('&', '&amp;')
                if '<' in target:
                    target = target.replace('<', '&lt;')
                if '>' in target:
                    target = target.replace('>', '&gt;')
                if quote in target:
                    target = target.replace(quote, '&#34;')


class ExpressionCompiler(object):
    """Internal wrapper around a TALES-engine.

    In addition to TALES expressions (strings which may appear wrapped
    in an ``Expression`` node), this compiler also supports other
    expression node types.

    Used internally be the compiler.
    """

    initial = COMPILER_INTERNALS_OR_DISALLOWED

    global_fallback = set(builtins.__dict__)

    def __init__(self, engine, cache, markers=None):
        self.engine = engine
        self.cache = cache
        self.markers = markers

    def __call__(self, expression, target):
        if isinstance(target, basestring):
            target = store(target)

        stmts = self.translate(expression, target)

        # Apply dynamic name rewrite transform to each statement
        transform = NameLookupRewriteTransformer(self._dynamic_transform)

        for stmt in stmts:
            transform(stmt)

        return stmts

    def translate(self, expression, target):
        if isinstance(target, basestring):
            target = store(target)

        cached = self.cache.get(expression)

        if cached is not None:
            stmts = [ast.Assign([target], cached)]
        elif isinstance(expression, ast.expr):
            stmts = [ast.Assign([target], expression)]
            self.cache[expression] = target
        else:
            # The engine interface supports simple strings, which
            # default to expression nodes
            if isinstance(expression, basestring):
                expression = Expression(expression)

            kind = type(expression).__name__
            visitor = getattr(self, "visit_%s" % kind)
            stmts = visitor(expression, target)

            # Add comment
            target_id = getattr(target, "id", target)
            comment = Comment(" %r -> %s" % (expression, target_id))
            stmts.insert(0, comment)

        return stmts

    @classmethod
    def _dynamic_transform(cls, node):
        name = node.id

        # Don't rewrite names that begin with an underscore; they are
        # internal and can be assumed to be locally defined. This
        # policy really should be part of the template program, not
        # defined here in the compiler.
        if name.startswith('_') or name in cls.initial:
            return node

        if isinstance(node.ctx, ast.Store):
            return store_econtext(name)

        # If the name is a Python global, first try acquiring it from
        # the dynamic context, then fall back to the global.
        if name in cls.global_fallback:
            return template(
                "econtext.get(key, name)",
                mode="eval",
                key=ast.Str(name),
                name=name
                )

        # Otherwise, simply acquire it from the dynamic context.
        return load_econtext(name)

    def visit_Expression(self, node, target):
        return self.engine(node.value, target)

    def visit_Negate(self, node, target):
        return self.translate(node.value, target) + \
               template("TARGET = not TARGET", TARGET=target)

    def visit_Marker(self, node, target):
        self.markers.add(node.name)

        return [ast.Assign([target], load("_marker_%s" % node.name))]

    def visit_Identity(self, node, target):
        expression = self.translate(node.expression, "_expression")
        value = self.translate(node.value, "_value")

        return expression + value + \
               template("TARGET = _expression is _value", TARGET=target)

    def visit_Equality(self, node, target):
        expression = self.translate(node.expression, "_expression")
        value = self.translate(node.value, "_value")

        return expression + value + \
               template("TARGET = _expression == _value", TARGET=target)

    def visit_Interpolation(self, node, target):
        def engine(expression, target):
            node = Expression(expression)
            return self.translate(node, target)

        expression = StringExpr(node.value)
        return expression(target, engine)

    def visit_Translate(self, node, target):
        if node.msgid is not None:
            msgid = ast.Str(node.msgid)
        else:
            msgid = target
        return self.translate(node.node, target) + \
               emit_translate(target, msgid, default=target)


class Compiler(object):
    """Generic compiler class.

    Iterates through nodes and yields Python statements which form a
    template program.
    """

    exceptions = NameError, \
                 ValueError, \
                 AttributeError, \
                 LookupError, \
                 TypeError

    defaults = {
        'translate': Symbol(fast_translate),
        'decode': Builtin("str"),
        'convert': Builtin("str"),
        }

    def __init__(self, engine):
        self._scopes = [set()]
        self._expression_cache = {}
        self._translations = []
        self._markers = set()

        self._engine = ExpressionCompiler(
            engine,
            self._expression_cache,
            self._markers
            )

    def __call__(self, program):
        # package as module
        module = ast.Module([])

        # For symbolic nodes (deprecated?)
        module.body += template("_marker = object()")

        # Visit template program
        module.body += self.visit(program)

        # Prepend module-wide marker values
        for marker in self._markers:
            module.body[:] = template(
                "MARKER = CLS()",
                MARKER=store("_marker_%s" % marker),
                CLS=Placeholder,
                ) + module.body

        ast.fix_missing_locations(module)
        generator = TemplateCodeGenerator(module)

        return generator.code

    def visit(self, node):
        if node is None:
            return ()
        kind = type(node).__name__
        visitor = getattr(self, "visit_%s" % kind)
        iterator = visitor(node)
        return list(iterator)

    def visit_Sequence(self, node):
        for item in node.items:
            for stmt in self.visit(item):
                yield stmt

    def visit_Element(self, node):
        for stmt in self.visit(node.start):
            yield stmt

        for stmt in self.visit(node.content):
            yield stmt

        if node.end is not None:
            for stmt in self.visit(node.end):
                yield stmt

    def visit_MacroProgram(self, node):
        for macro in node.macros:
            for stmt in self.visit(macro):
                yield stmt

        for stmt in self.visit_Macro(node):
            yield stmt

    def visit_Macro(self, node):
        body = []

        # Initialization
        body += template("append = stream.append")
        body += template("_i18n_domain = None")

        # Resolve defaults
        for name in self.defaults:
            body += template(
                "name = econtext[key]",
                name=name, key=ast.Str(name)
                )

        # Visit macro body
        body += itertools.chain(*tuple(map(self.visit, node.body)))

        function_name = "render" if node.name is None else \
                        "render_%s" % mangle(node.name)

        function = ast.FunctionDef(
            name=function_name, args=ast.arguments(
                args=[
                    param("stream"),
                    param("econtext"),
                    param("rcontext"),
                    ],
                defaults=(),
            ),
            body=body
            )

        yield function

    def visit_Text(self, node):
        return emit_node(ast.Str(node.value))

    def visit_Domain(self, node):
        backup = "_previous_i18n_domain_%d" % id(node)
        return template("BACKUP = _i18n_domain", BACKUP=backup) + \
               template("_i18n_domain = NAME", NAME=ast.Str(node.name)) + \
               self.visit(node.node) + \
               template("_i18n_domain = BACKUP", BACKUP=backup)

    def visit_OnError(self, node):
        body = []

        fallback = identifier("_fallback")
        body += template("fallback = len(stream)", fallback=fallback)

        body += [ast.TryExcept(
            body=self.visit(node.node),
            handlers=[ast.ExceptHandler(
                ast.Tuple(
                    [Builtin(cls.__name__) for cls in self.exceptions],
                    ast.Load()),
                None,
                template("del stream[fallback:]", fallback=fallback) + \
                self.visit(node.fallback),
                )]
            )]

        return body

    def visit_Content(self, node):
        name = "_content"
        body = self._engine(node.expression, store(name))

        # content conversion steps
        if node.msgid is not None:
            output = emit_translate(name, name)
        elif node.escape:
            output = emit_convert_and_escape(name, name)
        else:
            output = emit_convert(name)

        body += output
        body += template("if NAME is not None: append(NAME)", NAME=name)

        return body

    def visit_Interpolation(self, node):
        def escaping_engine(expression, target):
            node = Expression(expression)
            return self._engine(node, target) + \
                   emit_convert_and_escape(target, target)

        expression = StringExpr(node.value)

        name = identifier("content")

        return expression(store(name), escaping_engine) + \
               emit_node(load(name))

    def visit_Assignment(self, node):
        if len(node.names) != 1:
            target = ast.Tuple(
                [store_econtext(name)
                 for name in node.names], ast.Store
                )
        else:
            name = node.names[0]
            target = store_econtext(name)

        for name in node.names:
            if name in COMPILER_INTERNALS_OR_DISALLOWED:
                raise TranslationError(
                    "Name disallowed by compiler: %s." % name
                    )

        assignment = self._engine(node.expression, store("_value"))
        assignment += template("target = _value", target=target)

        for name in node.names:
            if not node.local:
                assignment += template(
                    "rcontext[KEY] = _value", KEY=ast.Str(name)
                    )

        return assignment

    def visit_Define(self, node):
        scope = set(self._scopes[-1])
        self._scopes.append(scope)

        for assignment in node.assignments:
            names = assignment.names
            local = assignment.local

            for stmt in self._enter_assignment(names, local):
                yield stmt

            for stmt in self.visit(assignment):
                yield stmt

        for stmt in self.visit(node.node):
            yield stmt

        for stmt in self._leave_assignment(names):
            yield stmt

        self._scopes.pop()

    def visit_Omit(self, node):
        return self.visit_Condition(node)

    def visit_Condition(self, node):
        target = "_condition"
        assignment = self._engine(node.expression, target)

        for stmt in assignment:
            yield stmt

        body = self.visit(node.node) or [ast.Pass()]

        orelse = getattr(node, "orelse", None)
        if orelse is not None:
            orelse = self.visit(orelse)

        test = load(target)

        yield ast.If(test, body, orelse)

    def visit_Translate(self, node):
        """Translation.

        Visit items and assign output to a default value.

        Finally, compile a translation expression and use either
        result or default.
        """

        body = []

        # Track the blocks of this translation
        self._translations.append(set())

        # Prepare new stream
        append = identifier("append", id(node))
        stream = identifier("stream", id(node))
        body += template("s = new_list", s=stream, new_list=LIST) + \
                template("a = s.append", a=append, s=stream)

        # Visit body to generate the message body
        code = self.visit(node.node)
        swap(ast.Suite(code), load(append), "append")
        body += code

        # Reduce white space and assign as message id
        msgid = identifier("msgid", id(node))
        body += template(
            "msgid = reduce_whitespace(''.join(stream)).strip()",
            msgid=msgid, stream=stream, reduce_whitespace=RE_REDUCE_WS)

        default = msgid

        # Compute translation block mapping if applicable
        names = self._translations[-1]
        if names:
            keys = []
            values = []

            for name in names:
                stream, append = self._get_translation_identifiers(name)
                keys.append(ast.Str(name))
                values.append(template("''.join(s)", s=stream, mode="eval"))

            mapping = ast.Dict(keys=keys, values=values)
        else:
            mapping = None

        # if this translation node has a name, use it as the message id
        if node.msgid:
            msgid = ast.Str(node.msgid)

        # emit the translation expression
        body += template(
            "append(translate("
            "msgid, mapping=mapping, default=default, domain=_i18n_domain))",
            msgid=msgid, default=default, mapping=mapping
            )

        # pop away translation block reference
        self._translations.pop()

        return body

    def visit_Start(self, node):
        line, column = node.prefix.location

        yield Comment(
            " %s%s ... (%d:%d)\n"
            " --------------------------------------------------------" % (
                node.prefix, node.name, line, column))

        for stmt in emit_node(ast.Str(node.prefix + node.name)):
            yield stmt

        for attribute in node.attributes:
            for stmt in self.visit(attribute):
                yield stmt

        for stmt in emit_node(ast.Str(node.suffix)):
            yield stmt

    def visit_End(self, node):
        for stmt in emit_node(ast.Str(
            node.prefix + node.name + node.space + node.suffix)):
            yield stmt

    def visit_Attribute(self, node):
        body = []

        target = identifier("attr", node.name)
        body += self._engine(node.expression, store(target)) + \
                emit_convert_and_escape(target, target,
                                        quote=ast.Str(node.quote)
                                        )

        f = node.space + node.name + node.eq + node.quote + "%s" + node.quote
        body += template(
            "if TARGET is not None: append(FORMAT % TARGET)",
            FORMAT=ast.Str(f),
            TARGET=target,
            )

        return body

    def visit_Cache(self, node):
        body = []

        for expression in node.expressions:
            name = identifier("cache", id(expression))
            target = store(name)

            # Skip re-evaluation
            if self._expression_cache.get(expression):
                continue

            body += self._engine(expression, target)
            self._expression_cache[expression] = target

        body += self.visit(node.node)

        return body

    def visit_UseInternalMacro(self, node):
        if node.name is None:
            render = "render"
        else:
            render = "render_%s" % mangle(node.name)

        return template("f(stream, econtext.copy(), rcontext)", f=render) + \
               template("econtext.update(rcontext)")

    def visit_DefineSlot(self, node):
        name = "_slot_%s" % mangle(node.name)
        body = self.visit(node.node)

        return [
            ast.TryExcept(
                body=template("_slot = econtext.pop(NAME)", NAME=ast.Str(name)),
                handlers=[ast.ExceptHandler(
                    None,
                    None,
                    body or [ast.Pass()],
                    )],
                orelse=template("_slot(stream, econtext.copy(), econtext)"),
                )
            ]

    def visit_Name(self, node):
        """Translation name."""

        if self._translations is None:
            raise TranslationError(
                "Not allowed outside of translation.", node.name)

        if node.name in self._translations[-1]:
            raise TranslationError(
                "Duplicate translation name: %s." % node.name)

        self._translations[-1].add(node.name)
        body = []

        # prepare new stream
        stream, append = self._get_translation_identifiers(node.name)
        body += template("s = new_list", s=stream, new_list=LIST) + \
                template("a = s.append", a=append, s=stream)

        # generate code
        code = self.visit(node.node)
        swap(ast.Suite(code), load(append), "append")
        body += code

        # output msgid
        text = Text('${%s}' % node.name)
        body += self.visit(text)

        return body

    def visit_UseMacro(self, node):
        callbacks = []

        for slot in node.slots:
            name = "_slot_%s" % mangle(slot.name)

            body = self.visit(slot.node)

            callbacks.append(
                ast.FunctionDef(
                    name=name,
                    args=ast.arguments(
                        args=[
                            param("stream"),
                            param("econtext"),
                            param("rcontext"),
                            ],
                        defaults=[],
                        ),
                    body=body or [ast.Pass()],
                ))

            callbacks += template(
                "econtext[KEY] = NAME", NAME=name, KEY=ast.Str(name)
                )

        assignment = self._engine(node.expression, store("_macro"))

        return (
            callbacks + \
            assignment + \
            template("_macro.include(stream, econtext.copy(), rcontext)") + \
            template("econtext.update(rcontext)")
            )

    def visit_Repeat(self, node):
        # Used for loop variable definition and restore
        self._scopes.append(set())

        # Variable assignment and repeat key for single- and
        # multi-variable repeat clause
        if node.local:
            contexts = "econtext",
        else:
            contexts = "econtext", "rcontext"

        if len(node.names) > 1:
            targets = [
                ast.Tuple([
                    subscript(fast_string(name), load(context), ast.Store())
                    for name in node.names], ast.Store)
                for context in contexts
                ]

            key = ast.Tuple([ast.Str(name) for name in node.names], ast.Load())
        else:
            name = node.names[0]
            targets = [
                subscript(fast_string(name), load(context), ast.Store())
                for context in contexts
                ]

            key = ast.Str(node.names[0])

        index = identifier("_index", id(node))
        assignment = [ast.Assign(targets, load("_item"))]

        # Make repeat assignment in outer loop
        names = node.names
        local = node.local
        outer = list(self._enter_assignment(names, local)) + \
                self._engine(node.expression, store("_iterator"))

        outer += template(
            "_iterator, INDEX = econtext['repeat'](key, _iterator)",
            key=key, INDEX=index
            )

        # Set a trivial default value for each name assigned to make
        # sure we assign a value even if the iteration is empty
        outer += [ast.Assign([
            store_econtext(name)
            for name in node.names], Builtin("None"))
                  ]

        # Compute inner body
        inner = self.visit(node.node)

        # After each iteration, decrease the index
        inner += template("index -= 1", index=index)

        # For items up to N - 1, emit repeat whitespace
        inner += template(
            "if INDEX > 0: append(WHITESPACE)",
            INDEX=index, WHITESPACE=ast.Str(node.whitespace)
            )

        # Main repeat loop
        outer += [ast.For(
            target=store("_item"),
            iter=load("_iterator"),
            body=assignment + inner,
            )]

        # Finally, clean up assignment
        outer += self._leave_assignment(names)
        self._scopes.pop()

        return outer

    def _get_translation_identifiers(self, name):
        assert self._translations
        prefix = id(self._translations[-1])
        stream = identifier("stream_%d" % prefix, name)
        append = identifier("append_%d" % prefix, name)
        return stream, append

    def _enter_assignment(self, names, local):
        for name in names:
            for stmt in template(
                "BACKUP = econtext.get(KEY, _marker)",
                BACKUP=identifier("backup_%s" % name, id(names)),
                KEY=ast.Str(fast_string(name)),
                ):
                yield stmt

    def _leave_assignment(self, names):
        for name in names:
            for stmt in template(
                "if BACKUP is _marker: del econtext[KEY]\n"
                "else:                 econtext[KEY] = BACKUP",
                BACKUP=identifier("backup_%s" % name, id(names)),
                KEY=ast.Str(fast_string(name)),
                ):
                yield stmt
