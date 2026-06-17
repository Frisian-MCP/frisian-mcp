# Roadmap

## Overview
Current state is for Django only installs by dynamically finding the DRF and viewSets in a brownfield install.  Greenfield installs are designed to be agent first and treat the agent as a user instead of a consumer.   The purpose of the project is to solve the context bloat issue that agents/LLMs get when connecting to an external tool.  The current setup drops all tools and data on the agent and fills the agent's context, reducing or crushing an agent's ability to reason with the data it gets.

Almost every solution that is out is either client side or proxy/middleware solution.  This project treats the issue as a server-side problem, as this is where the tools live.  The argument of this being a client side problem due to the client (agents) being the ones using tokens is valid to a sense, but those tokens are spent on the quality of the tool.   You don't change the browser to fit the website, you change the tool to fit the user.  In the case of MCP the user is an agent or LLM.

The Proxy/Middleware solution may work for now, but it comes with significant tech debt.  This will only grow as this field expands and becomes more complex.  The tools must be created and served through the server, then registered through the appropriate middleware solution, all while adding an additional complexity and another point of failure.

### The 4 MCP Token Bloat Issues (So far)
- **Dispatcher** -- To prevent the agents from being overwhelmed on connection a dispatcher pattern is in place.   Rather than expose every tool the server has the dispatcher exposes a small set of tools.  Think of needing a 10mm socket, but when you go to the tool room every tool is spread across the floor.  The dispatcher is the equivalent of all the tools being put into tool boxes, the hammers in the hammer box, sockets in the socket box, and so on.

- **Large Responses (Read)** -- The second issue is that the server will gladly give the agent what it wants, regardless of size of the response.  What the `@mcp_heavy` does is paginate large responses.  It can be configured by viewSet/DRF or in the system equivalent of `settings.py` as an auto-detect based on size.   In the same scenario as above it is like finding the sockets box from the dispatcher, but when you open it all of the tools are thrown out of the box.  What the `heavy` does would be like putting all of the sockets into trays according to size, then flipping through to find the section you need.

- **Large Responses (create, update, destroy)** -- In the traditional API setups many servers return the full data set back to the API call along with the status code after a write operation.  This is counterproductive to how agents operate and would fill up the context quickly when an agent is updating a system.  In the same scenario you bought a new set of sockets and you want to add them to your tool boxes.   When you put them in the tool box the new socket set is given back to you so you can validate it is correct.  The agent needs the status code to validate, and on some occasions a UUID or URL.  It is on the server to produce the right status code that the write operation succeeded or failed, same in an API call.  The agent has the option to view the verification, or it can just accept the status code.  This way the agent can decide if the context is worth the verification cost or to accept the status code.  In testing Nautobot the create device returns a large response to the agent making the network build costly.  The agents could still reason about small/medium builds but larger builds could introduce problems if not resolved.

- **Repeated Path Output** -- When an agent calls a path it gets a `tool/list` along with `help` options.  This is great for the agent not familiar with the system as it explores and learns.  It does come with a small cost, but that small cost adds up over repeated calls.  Example would be an agent making a build on a Nautobot system.  Every Location, device, IP address, DNS record, etc., that the agent hits it gets the `tool/list` and `help` options.  The planned solution for this is a `/mcp-lite` that is an overlay of the `/mcp` path.  This is not new routes that need to be maintained on the server, but has a difference that it returns no `tool/list` or `help` options.  If the agent needs to re-orient itself the `/mcp` path is available to the agent to call.


## Systems Tested
Frisian-MCP has been validated against four large, production-grade open-source Django applications spanning three unrelated domains:
 - **Nautobot** - network automation and source-of-truth (634+ API operations)
 - **NetBox** - infrastructure resource modeling
 - **Paperless-ngx** - document management
 - **Opend edX** - learning management at platform scale


### Why these systems?
These are among the largest and most complex open-source Django projects available, and they were chosen precisely because they look nothing like each other. Network modeling, document workflows, and online education impose entirely different data shapes, permission models, and API surfaces. Frisian-MCP integrated with each through the same mechanism — reading the existing DRF/OpenAPI schema, no changes to the host application's code.

The scale is what stresses the design. Nautobot alone exposes 634+ API operations; a flat MCP server would dump roughly 240,000 tokens of tool schema into an agent's context just to describe what's available, before any real work begins. Frisian-MCP's dispatcher pattern reduces that to about 2,000 tokens — a ~99% reduction — while still giving the agent access to every operation through progressive discovery.

If Frisian-MCP can compress and serve surfaces this large and this varied without overwhelming an agent, it can handle the needs of substantially simpler applications. The pattern isn't specific to any one domain. It's specific to the problem every sufficiently large Django API runs into: there's more surface than an agent can hold in context at once.

 ### Test it Yourself
 View the integration documents, ADR, Security, and more.  Just point your agent or LLM at the endpoint and let it validate the claims itself.
 `https://frisian-mcp.com/mcp`


## Greenfield (New build from scratch)
Beyond these brownfield integrations, Frisian-MCP also supports greenfield, agent-first builds where the system is designed for agents as users from the start.

## Summary
Django is the proof of concept that the patterns and break down of the problem work.  Other platforms can follow if shown to be successful.