-- Minimal test runner: luau luau/tests/run.luau
local Player = require("../src/Player")
local Shop = require("../src/Shop")

local passed, failed = 0, 0
local function test(name: string, fn: () -> ())
	local ok, err = pcall(fn)
	if ok then
		passed += 1
		print("ok   " .. name)
	else
		failed += 1
		print("FAIL " .. name .. ": " .. tostring(err))
	end
end

test("a new player starts with no coins", function()
	assert(Player.new("ada").coins == 0)
end)

test("picking up an item only affects that player", function()
	local a = Player.new("ada")
	local b = Player.new("bob")
	a:addItem("sword")
	assert(a:hasItem("sword"), "ada should have the sword")
	assert(not b:hasItem("sword"), "bob should not have ada's sword")
end)

test("shop prices", function()
	assert(Shop.priceOf("sword") == 100)
end)

print(string.format("%d passed, %d failed", passed, failed))
if failed > 0 then
	error("tests failed")
end
