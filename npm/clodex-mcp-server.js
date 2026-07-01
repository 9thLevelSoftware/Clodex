#!/usr/bin/env node
"use strict";

const { main } = require("./clodex.js");

process.exitCode = main(["mcp-server"]);
