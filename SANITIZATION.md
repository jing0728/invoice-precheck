# Sanitization Notes

This snapshot includes the local precheck application, rule engine, integration tools, and rule-reference documents. Credentials, browser profiles and cookies, historical reports, customer workbooks, production databases, logs, and original Git history are excluded. The original test suite was excluded because its cases used real customer information.

NetSuite account identifiers, local user paths, company identifiers, private network addresses, and example order/customer details were replaced in the supplied snapshot. Complete operation requires replacement workbooks and integration configuration. Queue scans contain write behavior; see the main README before connecting any live environment.

Prepared from the supplied sanitized snapshot dated October 28, 2026. New publication documentation is in English; existing source comments, interface text, and reference documents may retain their original language.
