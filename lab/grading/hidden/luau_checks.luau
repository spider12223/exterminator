-- Hidden checks for the Luau scenario. Copied into luau/tests/ by score.py and run with luau.
local results = {}
local function check(id: string, fn: () -> any)
	local ok, res = pcall(fn)
	if ok and res == true then
		table.insert(results, "RESULT " .. id .. " PASS")
	else
		table.insert(results, "RESULT " .. id .. " FAIL " .. tostring(res))
	end
end

check("S5-shared-default:seed", function()
	local Player = require("../src/Player")
	local a, b = Player.new("ada"), Player.new("bob")
	a:addItem("sword")
	return (a:hasItem("sword") and not b:hasItem("sword") and b.coins == 0) or "players share an inventory"
end)

check("S5-shared-default:pet-shallow-clone", function()
	local Pet = require("../src/Pet")
	local a, b = Pet.new("cat"), Pet.new("dog")
	Pet.addBuff(a, "speed")
	return (#a.buffs == 1 and #b.buffs == 0) or ("pets share buffs: b has " .. #b.buffs)
end)

check("S5-shared-default:quest-index-defaults", function()
	local Quest = require("../src/Quest")
	local a, b = Quest.new("intro"), Quest.new("boss")
	Quest.markStep(a, "talk")
	return (Quest.isStepDone(a, "talk") and not Quest.isStepDone(b, "talk")) or "quests share progress"
end)

check("S5-shared-default:decoy-settings", function()
	local Settings = require("../src/Settings")
	local a, b = Settings.new(), Settings.new()
	Settings.bind(a, "jump", "Space")
	return (a.keybinds.jump == "Space" and b.keybinds.jump == nil) or "settings broke"
end)

check("S5-shared-default:decoy-shop", function()
	local Shop = require("../src/Shop")
	return Shop.priceOf("sword") == 100 or "shop broke"
end)

print(table.concat(results, "\n"))
