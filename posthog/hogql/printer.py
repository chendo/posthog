import re
from dataclasses import dataclass
from datetime import datetime
from typing import List, Literal, Optional, Union, cast


from posthog.hogql import ast
from posthog.hogql.constants import (
    CLICKHOUSE_FUNCTIONS,
    HOGQL_AGGREGATIONS,
    MAX_SELECT_RETURNED_ROWS,
    HogQLSettings,
    ADD_TIMEZONE_TO_FUNCTIONS,
)
from posthog.hogql.context import HogQLContext
from posthog.hogql.database import Table, create_hogql_database
from posthog.hogql.errors import HogQLException
from posthog.hogql.escape_sql import (
    escape_clickhouse_identifier,
    escape_clickhouse_string,
    escape_hogql_identifier,
    escape_hogql_string,
)
from posthog.hogql.resolver import ResolverException, lookup_field_by_name, resolve_types
from posthog.hogql.transforms import expand_asterisks, resolve_lazy_tables
from posthog.hogql.transforms.macros import expand_macros
from posthog.hogql.transforms.property_types import resolve_property_types
from posthog.hogql.visitor import Visitor
from posthog.models.property import PropertyName, TableColumn
from posthog.utils import PersonOnEventsMode


def team_id_guard_for_table(table_type: Union[ast.TableType, ast.TableAliasType], context: HogQLContext) -> ast.Expr:
    """Add a mandatory "and(team_id, ...)" filter around the expression."""
    if not context.team_id:
        raise HogQLException("context.team_id not found")

    return ast.CompareOperation(
        op=ast.CompareOperationType.Eq,
        left=ast.Field(chain=["team_id"], type=ast.FieldType(name="team_id", table=table_type)),
        right=ast.Constant(value=context.team_id),
    )


def print_ast(
    node: ast.Expr,
    context: HogQLContext,
    dialect: Literal["hogql", "clickhouse"],
    stack: Optional[List[ast.SelectQuery]] = None,
    settings: Optional[HogQLSettings] = None,
) -> str:
    prepared_ast = prepare_ast_for_printing(node=node, context=context, dialect=dialect, stack=stack)
    return print_prepared_ast(node=prepared_ast, context=context, dialect=dialect, stack=stack, settings=settings)


def prepare_ast_for_printing(
    node: ast.Expr,
    context: HogQLContext,
    dialect: Literal["hogql", "clickhouse"],
    stack: Optional[List[ast.SelectQuery]] = None,
) -> ast.Expr:
    type = stack[-1].type if stack else None

    context.database = context.database or create_hogql_database(context.team_id)
    node = expand_macros(node, stack)
    resolve_types(node, context.database, type)
    expand_asterisks(node)
    if dialect == "clickhouse":
        node = resolve_property_types(node, context)
        resolve_lazy_tables(node, stack, context)

    # We add a team_id guard right before printing. It's not a separate step here.
    return node


def print_prepared_ast(
    node: ast.Expr,
    context: HogQLContext,
    dialect: Literal["hogql", "clickhouse"],
    stack: Optional[List[ast.SelectQuery]] = None,
    settings: Optional[HogQLSettings] = None,
) -> str:
    # _Printer also adds a team_id guard if printing clickhouse
    return _Printer(context=context, dialect=dialect, stack=stack or [], settings=settings).visit(node)


@dataclass
class JoinExprResponse:
    printed_sql: str
    where: Optional[ast.Expr] = None


class _Printer(Visitor):
    # NOTE: Call "print_ast()", not this class directly.

    def __init__(
        self,
        context: HogQLContext,
        dialect: Literal["hogql", "clickhouse"],
        stack: Optional[List[ast.AST]] = None,
        settings: Optional[HogQLSettings] = None,
    ):
        self.context = context
        self.dialect = dialect
        self.stack: List[ast.AST] = stack or []  # Keep track of all traversed nodes.
        self.settings = settings

    def visit(self, node: ast.AST):
        self.stack.append(node)
        response = super().visit(node)
        self.stack.pop()

        if len(self.stack) == 0 and self.dialect == "clickhouse" and self.settings:
            if not isinstance(node, ast.SelectQuery) and not isinstance(node, ast.SelectUnionQuery):
                raise HogQLException("Settings can only be applied to SELECT queries")
            settings = []
            for key, value in self.settings:
                if not isinstance(value, (int, float, str)):
                    raise HogQLException(f"Setting {key} must be a string, int, or float")
                if not re.match(r"^[a-zA-Z0-9_]+$", key):
                    raise HogQLException(f"Setting {key} is not supported")
                if isinstance(value, int) or isinstance(value, float):
                    settings.append(f"{key}={value}")
                else:
                    settings.append(f"{key}={self._print_escaped_string(value)}")
            if len(settings) > 0:
                response += f" SETTINGS {', '.join(settings)}"

        return response

    def visit_select_union_query(self, node: ast.SelectUnionQuery):
        query = " UNION ALL ".join([self.visit(expr) for expr in node.select_queries])
        if len(self.stack) > 1:
            return f"({query})"
        return query

    def visit_select_query(self, node: ast.SelectQuery):
        if self.dialect == "clickhouse":
            if not self.context.enable_select_queries:
                raise HogQLException("Full SELECT queries are disabled if context.enable_select_queries is False")
            if not self.context.team_id:
                raise HogQLException("Full SELECT queries are disabled if context.team_id is not set")

        # if we are the first parsed node in the tree, or a child of a SelectUnionQuery, mark us as a top level query
        part_of_select_union = len(self.stack) >= 2 and isinstance(self.stack[-2], ast.SelectUnionQuery)
        is_top_level_query = len(self.stack) <= 1 or (len(self.stack) == 2 and part_of_select_union)

        # We will add extra clauses onto this from the joined tables
        where = node.where

        joined_tables = []
        next_join = node.select_from
        while isinstance(next_join, ast.JoinExpr):
            if next_join.type is None:
                raise HogQLException("Printing queries with a FROM clause is not permitted before type resolution")

            visited_join = self.visit_join_expr(next_join)
            joined_tables.append(visited_join.printed_sql)

            # This is an expression we must add to the SELECT's WHERE clause to limit results, like the team ID guard.
            extra_where = visited_join.where
            if extra_where is None:
                pass
            elif isinstance(extra_where, ast.Expr):
                if where is None:
                    where = extra_where
                elif isinstance(where, ast.And):
                    where = ast.And(exprs=[extra_where] + where.exprs)
                else:
                    where = ast.And(exprs=[extra_where, where])
            else:
                raise HogQLException(f"Invalid where of type {type(extra_where).__name__} returned by join_expr")

            next_join = next_join.next_join

        columns = [self.visit(column) for column in node.select] if node.select else ["1"]
        where = self.visit(where) if where else None
        having = self.visit(node.having) if node.having else None
        prewhere = self.visit(node.prewhere) if node.prewhere else None
        group_by = [self.visit(column) for column in node.group_by] if node.group_by else None
        order_by = [self.visit(column) for column in node.order_by] if node.order_by else None

        clauses = [
            f"SELECT {'DISTINCT ' if node.distinct else ''}{', '.join(columns)}",
            f"FROM {' '.join(joined_tables)}" if len(joined_tables) > 0 else None,
            "PREWHERE " + prewhere if prewhere else None,
            "WHERE " + where if where else None,
            f"GROUP BY {', '.join(group_by)}" if group_by and len(group_by) > 0 else None,
            "HAVING " + having if having else None,
            f"ORDER BY {', '.join(order_by)}" if order_by and len(order_by) > 0 else None,
        ]

        limit = node.limit
        if self.context.limit_top_select and is_top_level_query:
            if limit is not None:
                if isinstance(limit, ast.Constant) and isinstance(limit.value, int):
                    limit.value = min(limit.value, MAX_SELECT_RETURNED_ROWS)
                else:
                    limit = ast.Call(name="min2", args=[ast.Constant(value=MAX_SELECT_RETURNED_ROWS), limit])
            else:
                limit = ast.Constant(value=MAX_SELECT_RETURNED_ROWS)

        if limit is not None:
            clauses.append(f"LIMIT {self.visit(limit)}")
            if node.offset is not None:
                clauses.append(f"OFFSET {self.visit(node.offset)}")
            if node.limit_by is not None:
                clauses.append(f"BY {', '.join([self.visit(expr) for expr in node.limit_by])}")
            if node.limit_with_ties:
                clauses.append("WITH TIES")

        response = " ".join([clause for clause in clauses if clause])

        # If we are printing a SELECT subquery (not the first AST node we are visiting), wrap it in parentheses.
        if not part_of_select_union and not is_top_level_query:
            response = f"({response})"

        return response

    def visit_join_expr(self, node: ast.JoinExpr) -> JoinExprResponse:
        # return constraints we must place on the select query
        extra_where: Optional[ast.Expr] = None

        join_strings = []

        if node.join_type is not None:
            join_strings.append(node.join_type)

        if isinstance(node.type, ast.TableAliasType):
            table_type = node.type.table_type
            if table_type is None:
                raise HogQLException(f"Table alias {node.type.name} does not resolve!")
            if not isinstance(table_type, ast.TableType):
                raise HogQLException(f"Table alias {node.type.name} does not resolve to a table!")

            if self.dialect == "clickhouse":
                table_name = table_type.table.clickhouse_table()
            else:
                table_name = table_type.table.hogql_table()
            join_strings.append(self._print_identifier(table_name))

            if node.alias is not None:
                join_strings.append(f"AS {self._print_identifier(node.alias)}")

            if self.dialect == "clickhouse":
                # TODO: do this in a separate pass before printing, along with person joins and other transforms
                extra_where = team_id_guard_for_table(node.type, self.context)

        elif isinstance(node.type, ast.TableType):
            if self.dialect == "clickhouse":
                join_strings.append(self._print_identifier(node.type.table.clickhouse_table()))
            else:
                join_strings.append(self._print_identifier(node.type.table.hogql_table()))

            if self.dialect == "clickhouse":
                # TODO: do this in a separate pass before printing, along with person joins and other transforms
                extra_where = team_id_guard_for_table(node.type, self.context)

        elif isinstance(node.type, ast.SelectQueryType):
            join_strings.append(self.visit(node.table))

        elif isinstance(node.type, ast.SelectUnionQueryType):
            join_strings.append(self.visit(node.table))

        elif isinstance(node.type, ast.SelectQueryAliasType) and node.alias is not None:
            join_strings.append(self.visit(node.table))
            join_strings.append(f"AS {self._print_identifier(node.alias)}")

        elif isinstance(node.type, ast.LazyTableType) and self.dialect == "hogql":
            join_strings.append(self._print_identifier(node.type.table.hogql_table()))

        else:
            raise HogQLException("Only selecting from a table or a subquery is supported")

        if node.table_final:
            join_strings.append("FINAL")

        if node.sample is not None:
            sample_clause = self.visit_sample_expr(node.sample)
            if sample_clause is not None:
                join_strings.append(sample_clause)

        if node.constraint is not None:
            join_strings.append(f"ON {self.visit(node.constraint)}")

        return JoinExprResponse(printed_sql=" ".join(join_strings), where=extra_where)

    def visit_binary_operation(self, node: ast.BinaryOperation):
        if node.op == ast.BinaryOperationType.Add:
            return f"plus({self.visit(node.left)}, {self.visit(node.right)})"
        elif node.op == ast.BinaryOperationType.Sub:
            return f"minus({self.visit(node.left)}, {self.visit(node.right)})"
        elif node.op == ast.BinaryOperationType.Mult:
            return f"multiply({self.visit(node.left)}, {self.visit(node.right)})"
        elif node.op == ast.BinaryOperationType.Div:
            return f"divide({self.visit(node.left)}, {self.visit(node.right)})"
        elif node.op == ast.BinaryOperationType.Mod:
            return f"modulo({self.visit(node.left)}, {self.visit(node.right)})"
        else:
            raise HogQLException(f"Unknown BinaryOperationType {node.op}")

    def visit_and(self, node: ast.And):
        return f"and({', '.join([self.visit(expr) for expr in node.exprs])})"

    def visit_or(self, node: ast.Or):
        return f"or({', '.join([self.visit(expr) for expr in node.exprs])})"

    def visit_not(self, node: ast.Not):
        return f"not({self.visit(node.expr)})"

    def visit_tuple(self, node: ast.Tuple):
        return f"tuple({', '.join([self.visit(expr) for expr in node.exprs])})"

    def visit_array_access(self, node: ast.ArrayAccess):
        return f"{self.visit(node.array)}[{self.visit(node.property)}]"

    def visit_array(self, node: ast.Array):
        return f"[{', '.join([self.visit(expr) for expr in node.exprs])}]"

    def visit_lambda(self, node: ast.Lambda):
        identifiers = [self._print_identifier(arg) for arg in node.args]
        if len(identifiers) == 0:
            raise ValueError("Lambdas require at least one argument")
        elif len(identifiers) == 1:
            return f"{identifiers[0]} -> {self.visit(node.expr)}"
        return f"({', '.join(identifiers)}) -> {self.visit(node.expr)}"

    def visit_order_expr(self, node: ast.OrderExpr):
        return f"{self.visit(node.expr)} {node.order}"

    def visit_compare_operation(self, node: ast.CompareOperation):
        left = self.visit(node.left)
        right = self.visit(node.right)
        if node.op == ast.CompareOperationType.Eq:
            if isinstance(node.right, ast.Constant) and node.right.value is None:
                return f"isNull({left})"
            else:
                return f"equals({left}, {right})"
        elif node.op == ast.CompareOperationType.NotEq:
            if isinstance(node.right, ast.Constant) and node.right.value is None:
                return f"isNotNull({left})"
            else:
                return f"notEquals({left}, {right})"
        elif node.op == ast.CompareOperationType.Gt:
            return f"greater({left}, {right})"
        elif node.op == ast.CompareOperationType.GtE:
            return f"greaterOrEquals({left}, {right})"
        elif node.op == ast.CompareOperationType.Lt:
            return f"less({left}, {right})"
        elif node.op == ast.CompareOperationType.LtE:
            return f"lessOrEquals({left}, {right})"
        elif node.op == ast.CompareOperationType.Like:
            return f"like({left}, {right})"
        elif node.op == ast.CompareOperationType.ILike:
            return f"ilike({left}, {right})"
        elif node.op == ast.CompareOperationType.NotLike:
            return f"not(like({left}, {right}))"
        elif node.op == ast.CompareOperationType.NotILike:
            return f"not(ilike({left}, {right}))"
        elif node.op == ast.CompareOperationType.In:
            return f"in({left}, {right})"
        elif node.op == ast.CompareOperationType.NotIn:
            return f"not(in({left}, {right}))"
        elif node.op == ast.CompareOperationType.Regex:
            return f"match({left}, {right})"
        elif node.op == ast.CompareOperationType.NotRegex:
            return f"not(match({left}, {right}))"
        else:
            raise HogQLException(f"Unknown CompareOperationType: {type(node.op).__name__}")

    def visit_constant(self, node: ast.Constant):
        if self.dialect == "clickhouse" and (
            isinstance(node.value, str) or isinstance(node.value, list) or isinstance(node.value, tuple)
        ):
            # inline the string in hogql, but use %(hogql_val_0)s in clickhouse
            key = f"hogql_val_{len(self.context.values)}"
            self.context.values[key] = node.value
            return f"%({key})s"
        else:
            return self._print_escaped_string(node.value)

    def visit_field(self, node: ast.Field):
        original_field = ".".join([self._print_identifier(identifier) for identifier in node.chain])
        if node.type is None:
            raise HogQLException(f"Field {original_field} has no type")

        if self.dialect == "hogql":
            if node.chain == ["*"]:
                return "*"
            # When printing HogQL, we print the properties out as a chain as they are.
            return ".".join([self._print_identifier(identifier) for identifier in node.chain])

        if node.type is not None:
            return self.visit(node.type)
        else:
            raise HogQLException(f"Unknown Type, can not print {type(node.type).__name__}")

    def visit_call(self, node: ast.Call):
        if node.name in HOGQL_AGGREGATIONS:
            required_arg_count = HOGQL_AGGREGATIONS[node.name]

            if isinstance(required_arg_count, int) and required_arg_count != len(node.args):
                raise HogQLException(
                    f"Aggregation '{node.name}' requires {required_arg_count} argument{'s' if required_arg_count != 1 else ''}, found {len(node.args)}"
                )
            if isinstance(required_arg_count, tuple) and (
                len(node.args) < required_arg_count[0] or len(node.args) > required_arg_count[1]
            ):
                raise HogQLException(
                    f"Aggregation '{node.name}' requires between {required_arg_count[0]} and {required_arg_count[1]} arguments, found {len(node.args)}"
                )

            # check that we're not running inside another aggregate
            for stack_node in self.stack:
                if stack_node != node and isinstance(stack_node, ast.Call) and stack_node.name in HOGQL_AGGREGATIONS:
                    raise HogQLException(
                        f"Aggregation '{node.name}' cannot be nested inside another aggregation '{stack_node.name}'."
                    )

            translated_args = ", ".join([self.visit(arg) for arg in node.args])
            if node.distinct:
                translated_args = f"DISTINCT {translated_args}"

            return f"{node.name}({translated_args})"

        elif node.name in CLICKHOUSE_FUNCTIONS:
            clickhouse_name, min_args, max_args = CLICKHOUSE_FUNCTIONS[node.name]
            args = [self.visit(arg) for arg in node.args]

            if min_args is not None and len(args) < min_args:
                if min_args == max_args:
                    raise HogQLException(f"Function '{node.name}' expects {min_args} arguments. Passed {len(args)}.")
                raise HogQLException(
                    f"Function '{node.name}' expects at least {min_args} arguments. Passed {len(args)}."
                )

            if max_args is not None and len(args) > max_args:
                if min_args == max_args:
                    raise HogQLException(f"Function '{node.name}' expects {max_args} arguments. Passed {len(args)}.")
                raise HogQLException(
                    f"Function '{node.name}' expects at most least {max_args} arguments. Passed {len(args)}."
                )

            if self.dialect == "clickhouse":
                if node.name in ADD_TIMEZONE_TO_FUNCTIONS:
                    args.append(self.visit(ast.Constant(value=self._get_timezone())))
                return f"{clickhouse_name}({', '.join(args)})"
            else:
                return f"{node.name}({', '.join(args)})"
        else:
            raise HogQLException(f"Unsupported function call '{node.name}(...)'")

    def visit_placeholder(self, node: ast.Placeholder):
        raise HogQLException(f"Found a Placeholder {{{node.field}}} in the tree. Can't generate query!")

    def visit_alias(self, node: ast.Alias):
        inside = self.visit(node.expr)
        if isinstance(node.expr, ast.Alias):
            inside = f"({inside})"
        return f"{inside} AS {self._print_identifier(node.alias)}"

    def visit_table_type(self, type: ast.TableType):
        if self.dialect == "clickhouse":
            return self._print_identifier(type.table.clickhouse_table())
        else:
            return self._print_identifier(type.table.hogql_table())

    def visit_table_alias_type(self, type: ast.TableAliasType):
        return self._print_identifier(type.name)

    def visit_lambda_argument_type(self, type: ast.LambdaArgumentType):
        return self._print_identifier(type.name)

    def visit_field_type(self, type: ast.FieldType):
        try:
            last_select = self._last_select()
            type_with_name_in_scope = lookup_field_by_name(last_select.type, type.name) if last_select else None
        except ResolverException:
            type_with_name_in_scope = None

        if (
            isinstance(type.table, ast.TableType)
            or isinstance(type.table, ast.TableAliasType)
            or isinstance(type.table, ast.VirtualTableType)
        ):
            resolved_field = type.resolve_database_field()
            if resolved_field is None:
                raise HogQLException(f'Can\'t resolve field "{type.name}" on table.')
            if isinstance(resolved_field, Table):
                if isinstance(type.table, ast.VirtualTableType):
                    return self.visit(ast.AsteriskType(table=ast.TableType(table=resolved_field)))
                else:
                    return self.visit(
                        ast.AsteriskType(
                            table=ast.TableAliasType(
                                table_type=ast.TableType(table=resolved_field), name=type.table.name
                            )
                        )
                    )

            # :KLUDGE: Legacy person properties handling. Only used within non-HogQL queries, such as insights.
            if (
                self.context.within_non_hogql_query
                and isinstance(type.table, ast.VirtualTableType)
                and type.name == "properties"
                and type.table.field == "poe"
            ):
                if self.context.person_on_events_mode != PersonOnEventsMode.DISABLED:
                    field_sql = "person_properties"
                else:
                    field_sql = "person_props"

            else:
                field_sql = self._print_identifier(resolved_field.name)
                if self.context.within_non_hogql_query and type_with_name_in_scope == type:
                    # Do not prepend table name in non-hogql context. We don't know what it actually is.
                    return field_sql
                field_sql = f"{self.visit(type.table)}.{field_sql}"

        elif isinstance(type.table, ast.SelectQueryType) or isinstance(type.table, ast.SelectQueryAliasType):
            field_sql = self._print_identifier(type.name)
            if isinstance(type.table, ast.SelectQueryAliasType):
                field_sql = f"{self.visit(type.table)}.{field_sql}"

            # :KLUDGE: Legacy person properties handling. Only used within non-HogQL queries, such as insights.
            if self.context.within_non_hogql_query and field_sql == "events__pdi__person.properties":
                if self.context.person_on_events_mode != PersonOnEventsMode.DISABLED:
                    field_sql = "person_properties"
                else:
                    field_sql = "person_props"

        else:
            raise HogQLException(f"Unknown FieldType table type: {type(type.table).__name__}")

        return field_sql

    def visit_property_type(self, type: ast.PropertyType):
        if type.joined_subquery is not None and type.joined_subquery_field_name is not None:
            return f"{self._print_identifier(type.joined_subquery.alias)}.{self._print_identifier(type.joined_subquery_field_name)}"

        field_type = type.parent
        field = field_type.resolve_database_field()

        # check for a materialised column
        table = field_type.table
        while isinstance(table, ast.TableAliasType):
            table = table.table_type

        # find a materialized property for the first part of the chain
        materialized_property_sql: Optional[str] = None
        if isinstance(table, ast.TableType):
            if self.dialect == "clickhouse":
                table_name = table.table.clickhouse_table()
            else:
                table_name = table.table.hogql_table()
            if field is None:
                raise HogQLException(f"Can't resolve field {field_type.name} on table {table_name}")
            field_name = cast(Union[Literal["properties"], Literal["person_properties"]], field.name)

            materialized_column = self._get_materialized_column(table_name, type.chain[0], field_name)
            if materialized_column:
                property_sql = self._print_identifier(materialized_column)
                if not self.context.within_non_hogql_query:
                    property_sql = f"{self.visit(field_type.table)}.{property_sql}"
                materialized_property_sql = property_sql
        elif (
            self.context.within_non_hogql_query
            and (isinstance(table, ast.SelectQueryAliasType) and table.alias == "events__pdi__person")
            or (isinstance(table, ast.VirtualTableType) and table.field == "poe")
        ):
            # :KLUDGE: Legacy person properties handling. Only used within non-HogQL queries, such as insights.
            if self.context.person_on_events_mode != PersonOnEventsMode.DISABLED:
                materialized_column = self._get_materialized_column("events", type.chain[0], "person_properties")
            else:
                materialized_column = self._get_materialized_column("person", type.chain[0], "properties")
            if materialized_column:
                materialized_property_sql = self._print_identifier(materialized_column)

        if materialized_property_sql is not None:
            if len(type.chain) == 1:
                return materialized_property_sql
            else:
                args = [materialized_property_sql]
                for name in type.chain[1:]:
                    key = f"hogql_val_{len(self.context.values)}"
                    self.context.values[key] = name
                    args.append(f"%({key})s")
                return trim_quotes_expr(f"JSONExtractRaw({', '.join(args)})")

        args = [self.visit(field_type)]
        for name in type.chain:
            key = f"hogql_val_{len(self.context.values)}"
            self.context.values[key] = name
            args.append(f"%({key})s")
        return trim_quotes_expr(f"JSONExtractRaw({', '.join(args)})")

    def visit_sample_expr(self, node: ast.SampleExpr):
        sample_value = self.visit_ratio_expr(node.sample_value)
        offset_clause = ""
        if node.offset_value:
            offset_value = self.visit_ratio_expr(node.offset_value)
            offset_clause = f" OFFSET {offset_value}"

        return f"SAMPLE {sample_value}{offset_clause}"

    def visit_ratio_expr(self, node: ast.RatioExpr):
        return self.visit(node.left) if node.right is None else f"{self.visit(node.left)}/{self.visit(node.right)}"

    def visit_select_query_alias_type(self, type: ast.SelectQueryAliasType):
        return self._print_identifier(type.alias)

    def visit_field_alias_type(self, type: ast.FieldAliasType):
        return self._print_identifier(type.name)

    def visit_virtual_table_type(self, type: ast.VirtualTableType):
        return self.visit(type.table)

    def visit_asterisk_type(self, type: ast.AsteriskType):
        return "*"

    def visit_lazy_join_type(self, type: ast.LazyJoinType):
        raise HogQLException("Unexpected ast.LazyJoinType. Make sure LazyJoinResolver has run on the AST.")

    def visit_lazy_table_type(self, type: ast.LazyJoinType):
        raise HogQLException("Unexpected ast.LazyTableType. Make sure LazyJoinResolver has run on the AST.")

    def visit_field_traverser_type(self, type: ast.FieldTraverserType):
        raise HogQLException("Unexpected ast.FieldTraverserType. This should have been resolved.")

    def visit_unknown(self, node: ast.AST):
        raise HogQLException(f"Unknown AST node {type(node).__name__}")

    def _last_select(self) -> Optional[ast.SelectQuery]:
        """Find the last SELECT query in the stack."""
        for node in reversed(self.stack):
            if isinstance(node, ast.SelectQuery):
                return node
        return None

    def _print_identifier(self, name: str) -> str:
        if self.dialect == "clickhouse":
            return escape_clickhouse_identifier(name)
        return escape_hogql_identifier(name)

    def _print_escaped_string(self, name: float | int | str | list | tuple | datetime) -> str:
        if self.dialect == "clickhouse":
            return escape_clickhouse_string(name, timezone=self._get_timezone())
        return escape_hogql_string(name, timezone=self._get_timezone())

    def _get_materialized_column(
        self, table_name: str, property_name: PropertyName, field_name: TableColumn
    ) -> Optional[str]:
        try:
            from ee.clickhouse.materialized_columns.columns import (
                TablesWithMaterializedColumns,
                get_materialized_columns,
            )

            materialized_columns = get_materialized_columns(cast(TablesWithMaterializedColumns, table_name))
            return materialized_columns.get((property_name, field_name), None)
        except ModuleNotFoundError:
            return None

    def _get_timezone(self):
        return self.context.database.get_timezone() if self.context.database else "UTC"


def trim_quotes_expr(expr: str) -> str:
    return f"replaceRegexpAll({expr}, '^\"|\"$', '')"
