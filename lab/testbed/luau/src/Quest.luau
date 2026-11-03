local QuestDefaults = {
	completed = false,
	progress = {},
}

local Quest = {}

function Quest.new(name: string)
	return setmetatable({ name = name }, { __index = QuestDefaults })
end

function Quest.markStep(quest, step: string)
	quest.progress[step] = true
end

function Quest.isStepDone(quest, step: string): boolean
	return quest.progress[step] == true
end

return Quest
